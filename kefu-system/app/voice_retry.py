# -*- coding: utf-8 -*-
"""语音补转写队列：兜住「消息先到、音频后到」的微信语音。

背景（实测）：微信收到语音消息时只先写消息行（``message_*.db``），音频
（``media_*.db`` 的 ``VoiceInfo.voice_data``）要过一段时间、或客户端在某次
打开会话/播放后才真正落库。而 Listener 是秒级轮询，语音消息一到达就立刻
转写，几乎必然撞上「音频还没落库」——表现为偶发的「未找到语音数据」，
同一条语音过几分钟再看就能识别。

因此转写失败（仅限「取不到音频」这类可恢复原因）不能当终态丢弃，而要按
退避序列排队重试：

- 前几次（5s / 15s）足够快，能赶在合并窗口关闭前后补上，用户几乎无感；
- 后几次（45s / 120s / 300s）覆盖「用户过一会儿才在微信里点开该语音」；
- 全部用尽才落终态，并由文案引导用户手动重试。

设计约束：
- 单后台线程 + 条件变量，队列按到期时间排序（同一时刻只有个位数任务）；
- 同一 ``(username, local_id)`` 只允许一个在途任务（去重，避免日志刷屏）；
- 回调异常一律吞掉：补转写是附加能力，不能影响监听/分析主流程；
- 进程重启后队列丢失（内存态），但音频仍会随微信下载落库，用户在分析
  日志里手动「重新识别」即可补回。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 重试间隔（秒，相对「上一次尝试失败」）。累计跨度约 8 分钟。
DEFAULT_DELAYS: Tuple[float, ...] = (5.0, 15.0, 45.0, 120.0, 300.0)
# 队列上限：正常场景个位数；超过说明微信长期不落音频，丢弃最旧任务
MAX_PENDING = 200


class _Job:
    """一条待重试的语音（username + local_id 唯一）。"""

    __slots__ = ("username", "local_id", "payload", "attempts", "due",
                 "enqueued_at")

    def __init__(self, username: str, local_id: int, payload: dict,
                 due: float) -> None:
        self.username = username
        self.local_id = local_id
        self.payload = payload
        self.attempts = 0          # 已完成的失败尝试次数
        self.due = due             # 下次尝试的单调时钟时刻
        self.enqueued_at = time.monotonic()


class VoiceRetryQueue:
    """语音延迟重试队列（线程安全，单 worker）。

    Args:
        transcribe: ``(username, local_id) -> (text, error)``，通常直接传
            :meth:`WeChatService.transcribe_voice`。
        on_success: ``(username, local_id, text, payload, attempt) -> None``，
            第 ``attempt`` 次尝试成功时回调（attempt 从 1 开始）。
        on_failure: ``(username, local_id, error, payload, attempts) -> None``，
            所有重试用尽时回调。
        delays: 重试间隔序列（秒），长度即最大重试次数。
    """

    def __init__(self,
                 transcribe: Callable[[str, int], Tuple[str, str]],
                 on_success: Callable[[str, int, str, dict, int], None],
                 on_failure: Optional[
                     Callable[[str, int, str, dict, int], None]] = None,
                 delays: Tuple[float, ...] = DEFAULT_DELAYS) -> None:
        self._transcribe = transcribe
        self._on_success = on_success
        self._on_failure = on_failure
        self._delays = tuple(max(0.1, float(d)) for d in delays) or (5.0,)
        self._jobs: List[_Job] = []
        self._cv = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 统计（/api/status 展示；失败不影响功能）
        self._stats: Dict[str, int] = {
            "enqueued": 0, "succeeded": 0, "exhausted": 0, "dropped": 0,
        }
        self._attempts_total = 0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台重试线程（重复调用无副作用）。"""
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="kefu-voice-retry", daemon=True)
            self._thread.start()
        logger.info("语音补转写队列已启动（重试间隔 %s 秒）",
                    "/".join("%g" % d for d in self._delays))

    def stop(self) -> None:
        """停止后台线程（在途任务放弃；队列内容随进程结束丢弃）。"""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5)
            logger.info("语音补转写队列已停止（剩余 %d 条未重试）",
                        len(self._jobs))

    def enqueue(self, username: str, local_id: int, payload: dict) -> bool:
        """把一个待补转写的语音放入队列；重复入队返回 False。

        首次尝试延迟 ``delays[0]`` 秒（不立即重试：刚失败的一瞬间音频通常
        仍未落库，立刻再查毫无意义）。
        """
        username = str(username or "")
        try:
            local_id = int(local_id)
        except (TypeError, ValueError):
            return False
        if not username or local_id <= 0:
            return False
        with self._cv:
            for job in self._jobs:
                if job.username == username and job.local_id == local_id:
                    return False
            if len(self._jobs) >= MAX_PENDING:
                # 丢最旧的（它的音频大概率已彻底取不到），保证内存有界
                oldest = min(self._jobs, key=lambda j: j.enqueued_at)
                self._jobs.remove(oldest)
                self._stats["dropped"] += 1
                logger.warning("语音补转写队列已满（%d），丢弃最旧任务 %s/%d",
                               MAX_PENDING, oldest.username, oldest.local_id)
            self._jobs.append(_Job(username, local_id, dict(payload or {}),
                                   time.monotonic() + self._delays[0]))
            self._stats["enqueued"] += 1
            self._cv.notify_all()
        return True

    def pending_count(self) -> int:
        """在途任务数（含等待中的下一次尝试）。"""
        with self._cv:
            return len(self._jobs)

    def status(self) -> dict:
        """队列状态快照（运维/接口展示用）。"""
        with self._cv:
            pending = len(self._jobs)
            next_due = min((j.due for j in self._jobs), default=0.0)
            next_in = max(0.0, next_due - time.monotonic()) if pending else 0.0
            return {
                "pending": pending,
                "next_in_sec": int(round(next_in)),
                "attempts": self._attempts_total,
                "delays": list(self._delays),
                **self._stats,
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _run(self) -> None:
        """调度循环：等到最近任务的到期时刻 → 尝试 → 失败则重新排期。"""
        while not self._stop.is_set():
            job = self._wait_next()
            if job is None:
                continue
            try:
                self._attempt(job)
            except Exception:      # 绝不让调度线程因单条任务退出
                logger.exception("语音补转写任务异常（已忽略）")

    def _wait_next(self) -> Optional[_Job]:
        """阻塞直到有任务到期，取出并返回；停止时返回 None。"""
        with self._cv:
            while not self._stop.is_set():
                if not self._jobs:
                    self._cv.wait(timeout=1.0)
                    continue
                job = min(self._jobs, key=lambda j: j.due)
                wait = job.due - time.monotonic()
                if wait <= 0:
                    self._jobs.remove(job)
                    return job
                # 上限 1 秒：新增任务/停止信号能及时打断等待
                self._cv.wait(timeout=min(wait, 1.0))
        return None

    def _attempt(self, job: _Job) -> None:
        """执行一次转写尝试；失败则按退避序列重新入列或落终态。"""
        job.attempts += 1
        self._attempts_total += 1
        try:
            text, error = self._transcribe(job.username, job.local_id)
        except Exception as exc:   # transcribe 本身设计为不外抛，双保险
            logger.exception("语音补转写调用异常（%s/%d）",
                             job.username, job.local_id)
            text, error = "", f"转写调用异常：{exc}"
        if text:
            self._stats["succeeded"] += 1
            logger.info("语音补转写成功（%s local_id=%d，第 %d 次尝试，%d 字）",
                        job.username, job.local_id, job.attempts, len(text))
            self._safe_call(self._on_success, job.username, job.local_id,
                            text, job.payload, job.attempts)
            return
        error = error or "未知原因"
        # 先判「重试用尽」，再判「服务停止」：两者语义完全不同。服务关闭时
        # 在途任务只是被打断（重启后用户手动重试即可），不能落成「已重试
        # N 次仍未取到」的终态，否则一次重启就会把正常的语音误标成失败。
        if job.attempts >= len(self._delays):
            self._stats["exhausted"] += 1
            logger.info("语音补转写最终失败（%s local_id=%d，共尝试 %d 次）：%s",
                        job.username, job.local_id, job.attempts, error)
            if self._on_failure is not None:
                self._safe_call(self._on_failure, job.username, job.local_id,
                                error, job.payload, job.attempts)
            return
        if self._stop.is_set():
            logger.info("服务停止，放弃语音补转写（%s local_id=%d，已尝试 %d 次）",
                        job.username, job.local_id, job.attempts)
            return
        delay = self._delays[job.attempts]
        with self._cv:
            if self._stop.is_set():
                return
            job.due = time.monotonic() + delay
            self._jobs.append(job)
            self._cv.notify_all()
        logger.info("语音补转写第 %d 次未成功（%s local_id=%d）：%s；%.0fs 后重试",
                    job.attempts, job.username, job.local_id, error, delay)

    @staticmethod
    def _safe_call(fn, *args) -> None:
        """回调异常吞掉：补转写是附加能力，不能让主流程受影响。"""
        try:
            fn(*args)
        except Exception:
            logger.exception("语音补转写回调异常（已忽略）")

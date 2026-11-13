# -*- coding: utf-8 -*-
"""图片「不完整」延迟重试队列：兜住「消息先到、原图后到」的微信图片。

背景（实测）：微信收到图片消息时只先写消息行（``message_*.db``）与缩略图，
**原图**（``media_*.db`` 里的图片 data）要等客户端真正下载完才落库。而
Listener 是秒级轮询，图片消息一到达就立刻取图识别，经常撞上「原图还没下载
完」——表现为取不到图，或者解出来的图不完整（``vision.MSG_INCOMPLETE``）。
批次 6 的实现只把这种情况写一条 skipped 日志，于是**这张图永久漏识别**，
里面的卖货商机也就丢了。

因此「图片不完整 / 原图还没落库」不能当终态丢弃，而要按退避序列排队重试：

- 10s / 30s 两跳能赶上绝大多数「几秒到几十秒就下载完」的场景；
- 90s / 300s 覆盖「用户过一会儿才在微信里点开原图」（点开后才会下载原图）；
- 全部用尽才落终态日志，且**绝不写 image_analysis 缓存**（缓存只放「已经
  拿到模型结论」的行，否则会把这张图永久钉成「非配件」，额度/网络恢复后
  也不会再识别 —— 假阴性）。

设计约束：
- 单后台线程 + 条件变量，队列按到期时间排序（同一时刻只有个位数任务）；
- 同一 ``(username, local_id)`` 只允许一个在途任务（去重，避免日志刷屏与
  重复烧 token）；
- handler 返回语义三态：``"done"`` / ``"retry"`` / ``"abort"``，由调用方
  决定「是否值得再试」（额度/Key/未配置这类重试也没用，必须 ``"abort"``）；
- handler 抛异常一律吞掉并继续调度：重试是附加能力，不能影响监听/分析主流程；
- 进程重启后队列丢失（内存态），但原图仍会随微信下载落库，用户在分析日志里
  手动「重新识别」即可补回。

**与 voice_retry 同构，刻意不复用以免波及已上线的语音补转写**：语音那条链路
的返回值是 ``(text, error)`` + 成功/失败双回调，且已验证上线；图片这边需要
「三态决策」（额度不足要立刻放弃，不能反复烧 token），参数形态不同，强行
抽象成一个通用队列反而会把两条链路一起拖下水。所以这里复制结构、独立演进。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 重试间隔（秒，相对「上一次尝试失败」）。累计跨度约 7 分钟。
# 10s 起跳：微信原图通常在几秒到几十秒内下载完，太短会做无用功；
# 300s 收尾：覆盖「用户过一会儿才在微信里点开原图」这种最长尾的情况。
DEFAULT_DELAYS: Tuple[float, ...] = (10.0, 30.0, 90.0, 300.0)
# 队列上限：正常场景个位数；超过说明微信长期不落原图，丢弃最旧任务保证内存有界
MAX_PENDING = 100

# handler 返回值（语义固定，调用方必须三选一）
RESULT_DONE = "done"
RESULT_RETRY = "retry"
RESULT_ABORT = "abort"


class _Job:
    """一条待重试的图片（username + local_id 唯一）。"""

    __slots__ = ("username", "local_id", "payload", "attempts", "due",
                 "enqueued_at")

    def __init__(self, username: str, local_id: int, payload: dict,
                 due: float) -> None:
        self.username = username
        self.local_id = local_id
        self.payload = payload
        self.attempts = 0          # 已完成的尝试次数
        self.due = due             # 下次尝试的单调时钟时刻
        self.enqueued_at = time.monotonic()


class ImageRetryQueue:
    """图片延迟重试队列（线程安全，单 worker）。

    Args:
        handler: ``(task: dict) -> str``，执行一次重试尝试，返回
            ``"done"``（已有结论，结束）/ ``"retry"``（仍需重试，继续退避）/
            ``"abort"``（放弃，不再重试）。``task`` 是入队时 payload 的浅拷贝，
            额外带一个 ``retry_attempt`` 字段（从 1 开始），供调用方在日志里
            写明「第 N 次重试」。
        delays: 重试间隔序列（秒），长度即最大重试次数。
        max_pending: 队列上限，满则丢最旧任务。
        on_drop: ``(username: str, local_id: int, task: dict) -> None``，队列满
            丢弃最旧任务时回调（批次 7 QA-P3-2）。仅靠 ``logger.warning`` 用户
            在页面上看不到「哪张图被丢了」，交由调用方落一条可见日志；回调异常
            一律吞掉，绝不影响入队。默认 None（不回调）。
        on_give_up: ``(username: str, local_id: int, task: dict, attempts: int)
            -> None``，**重试用尽**时回调（第 13 批需求 7）。此前用尽后只在
            服务日志里留一行，用户看到最后一条「继续等待后自动补识别」会一直
            等下去；有了这个回调可以落一条「已放弃 + 怎么手动补」的可见日志。
            ``abort``（额度/Key 之类不值得重试）不走它，那条路径自己已落日志。
    """

    def __init__(self,
                 handler: Callable[[dict], str],
                 delays: Tuple[float, ...] = DEFAULT_DELAYS,
                 max_pending: int = MAX_PENDING,
                 on_drop: Optional[Callable[[str, int, dict], None]] = None,
                 on_give_up: Optional[Callable[[str, int, dict, int], None]] = None
                 ) -> None:
        self._handler = handler
        self._on_drop = on_drop
        self._on_give_up = on_give_up
        self._delays = tuple(max(0.1, float(d)) for d in delays) or (10.0,)
        self._max_pending = max(1, int(max_pending))
        self._jobs: List[_Job] = []
        self._cv = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 统计（/api/status 与排障展示；统计失败不影响功能）
        self._stats: Dict[str, int] = {
            "enqueued": 0,      # 入队成功次数
            "succeeded": 0,     # handler 返回 done
            "retried": 0,       # 已重新排期的次数（含最终用尽的那次之前）
            "abandoned": 0,     # handler 返回 abort + 重试用尽
            "dropped": 0,       # 队列满被丢弃
        }
        self._attempts_total = 0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台重试线程（重复调用无副作用；stop 后可再次启动）。"""
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="kefu-image-retry", daemon=True)
            self._thread.start()
        logger.info("图片补识别队列已启动（重试间隔 %s 秒）",
                    "/".join("%g" % d for d in self._delays))

    def stop(self) -> None:
        """停止后台线程（在途任务放弃；队列内容随进程结束丢弃）。

        语义（批次 7 QA-P3-5，刻意如此）：``stop`` **只停线程、不清空队列**。
        在途任务只是被打断，不会落成「已重试 N 次仍未取到」的终态——一次重启
        不该把正常图片误标成失败；队列本身是内存态，随进程退出自然消失，
        原图仍会随微信落库，用户在分析日志里手动「重新识别」即可补回。
        """
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
            thread = self._thread
            self._thread = None
            remaining = len(self._jobs)
        if thread is not None:
            thread.join(timeout=5)
        if remaining:
            logger.warning("图片补识别队列已停止，仍有 %d 条未重试", remaining)

    def enqueue(self, username: str, local_id: int, task: dict) -> bool:
        """把一张待重试的图片放入队列；重复入队/参数非法返回 False。

        首次尝试延迟 ``delays[0]`` 秒（不立即重试：刚失败的一瞬间原图通常
        仍未下载完，立刻再取毫无意义，还白占一次视觉调用）。

        非法参数（username 空 / local_id ≤ 0 / 非数字）被拒时**不回调 on_drop**
        ——那不是「被挤掉」，不该在用户页面上多一条日志。
        """
        username = str(username or "")
        try:
            local_id = int(local_id)
        except (TypeError, ValueError):
            return False
        if not username or local_id <= 0:
            return False
        dropped: Optional[_Job] = None
        with self._cv:
            for job in self._jobs:
                if job.username == username and job.local_id == local_id:
                    return False
            if len(self._jobs) >= self._max_pending:
                # 丢最旧的（它的原图大概率已彻底取不到），保证内存有界
                oldest = min(self._jobs, key=lambda j: j.enqueued_at)
                self._jobs.remove(oldest)
                self._stats["dropped"] += 1
                dropped = oldest
                logger.warning("图片补识别队列已满（%d），丢弃最旧任务 %s/%d",
                               self._max_pending, oldest.username,
                               oldest.local_id)
            self._jobs.append(_Job(username, local_id, dict(task or {}),
                                   time.monotonic() + self._delays[0]))
            self._stats["enqueued"] += 1
            self._cv.notify_all()
        # 回调放在锁外：调用方要写 analysis_logs（磁盘 IO），不能占着队列锁
        if dropped is not None and self._on_drop is not None:
            self._safe_call(self._on_drop, dropped.username, dropped.local_id,
                            dict(dropped.payload or {}))
        return True

    def pending_count(self) -> int:
        """在途任务数（**不含正在执行的那一条**）。

        语义（批次 7 QA-P3-4，刻意如此）：:meth:`_wait_next` 取到任务后会先把它
        从队列里移除再执行，所以正在跑的那一条不计入 ``pending``。它会短暂让
        ``pending`` 掉到 0（哪怕还有任务在飞），因此**不要**用 ``pending == 0``
        判定「全部完成」，要看 :meth:`status` 里的 ``succeeded``/``abandoned``。
        """
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
        """调度循环：等到最近任务的到期时刻 → 尝试 → 按返回语义排期/结束。"""
        while not self._stop.is_set():
            job = self._wait_next()
            if job is None:
                continue
            try:
                self._attempt(job)
            except Exception:      # 绝不让调度线程因单条任务退出
                logger.exception("图片补识别任务异常（已忽略）")

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
        """执行一次重试尝试，并按 handler 的三态返回值决定去留。"""
        job.attempts += 1
        self._attempts_total += 1
        payload = dict(job.payload or {})
        # 让调用方能在日志里写明「第 N 次重试」（N 从 1 开始）
        payload["retry_attempt"] = job.attempts
        decision = RESULT_RETRY
        try:
            decision = self._call_handler(payload)
        except Exception:      # handler 内部已兜异常，这里是双保险
            logger.exception("图片补识别调用异常（%s/%d，第 %d 次）",
                             job.username, job.local_id, job.attempts)
            # 异常按「可重试」处理：退避次数有限（delays 长度），不会死循环；
            # 一次偶发 bug 不该直接把这张图的商机判死。
            decision = RESULT_RETRY
        decision = str(decision or "").strip().lower()
        if decision == RESULT_DONE:
            self._stats["succeeded"] += 1
            logger.info("图片补识别完成（%s local_id=%d，第 %d 次尝试）",
                        job.username, job.local_id, job.attempts)
            return
        if decision == RESULT_ABORT:
            self._stats["abandoned"] += 1
            logger.info("图片补识别放弃（%s local_id=%d，第 %d 次尝试）："
                        "不值得继续重试", job.username, job.local_id,
                        job.attempts)
            return
        # 先判「重试用尽」，再判「服务停止」：两者语义完全不同。服务关闭时
        # 在途任务只是被打断（重启后用户手动重试即可），不能落成「已重试
        # N 次仍未取到」的终态，否则一次重启就会把正常图片误标成失败。
        if job.attempts >= len(self._delays):
            self._stats["abandoned"] += 1
            logger.info("图片补识别最终失败（%s local_id=%d，共尝试 %d 次）："
                        "原图始终未就绪", job.username, job.local_id,
                        job.attempts)
            if self._on_give_up is not None:
                # 回调放锁外：调用方要写 analysis_logs（磁盘 IO）
                self._safe_call(self._on_give_up, job.username, job.local_id,
                                dict(job.payload or {}), job.attempts)
            return
        if self._stop.is_set():
            logger.info("服务停止，放弃图片补识别（%s local_id=%d，已尝试 "
                        "%d 次）", job.username, job.local_id, job.attempts)
            return
        delay = self._delays[job.attempts]
        with self._cv:
            if self._stop.is_set():
                return
            job.due = time.monotonic() + delay
            self._jobs.append(job)
            self._stats["retried"] += 1
            self._cv.notify_all()
        logger.info("图片补识别第 %d 次未完成（%s local_id=%d）；%.0fs 后重试",
                    job.attempts, job.username, job.local_id, delay)

    def _call_handler(self, payload: dict) -> str:
        """调用 handler 并把非预期返回值归一成 ``"retry"``。"""
        result = self._handler(payload)
        if result is None:
            return RESULT_RETRY
        return str(result)

    @staticmethod
    def _safe_call(fn, *args) -> None:
        """回调异常吞掉：补识别是附加能力，不能让入队/主流程受影响。"""
        try:
            fn(*args)
        except Exception:
            logger.exception("图片补识别回调异常（已忽略）")

# -*- coding: utf-8 -*-
"""GUI 发送服务：独立线程串行消费发送队列。

流程（技术方案 4.2）：
    submit(SendTask) → SenderThread 取任务（同一时刻仅一个 GUI 操作）
    → WeChatGUI 单例（线程内创建）→ is_alive / ensure_visible（置前统一在此）
    → open_chat（_chat_open_confirmed 校验）
    → at_member / quote_msg / send_msg(verify=True 数据库确认)
    → 更新商机为已回复 → WS 广播 send_result

失败映射为明确中文错误（PRD P0-8），任何异常不得终止线程。
"""
from __future__ import annotations

import html
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.db import Database
from app.notifier import Notifier

logger = logging.getLogger(__name__)

# 引用定位用的文本长度上限：太长会 OCR/匹配不到，太短容易误命中其它消息
QUOTE_SNIPPET_MAX = 24
# 无可识别文字的占位内容（图片/文件/小程序等），无法靠文本定位消息行
_PLACEHOLDER_RE = re.compile(r"^\[[^\]]{1,12}\]")
# GUI 操作互斥锁等待上限（秒）：发送线程与「跳转微信」API 线程共用同一把
# 锁，同一时刻只允许一个流程动微信窗口。发送全程（含打开会话、DB 回读
# 确认）实测 5~20s，60s 足够覆盖慢网/慢机场景。
GUI_LOCK_TIMEOUT_SEC = 60


class QuoteTargetUnavailable(Exception):
    """要引用的消息既不是最新一条、又没有可识别文字（无法在列表里定位）。"""


class _Stages:
    """发送链路分段计时（回答「到底慢在哪一段」）。"""

    LABELS = {
        "init": "组件初始化",
        "front": "前置窗口",
        "open": "打开会话",
        "send": "输入发送",
        "fail": "失败前",
    }

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._last = self._t0
        self.ms: Dict[str, int] = {}

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self.ms[name] = self.ms.get(name, 0) + int((now - self._last) * 1000)
        self._last = now

    def total_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def summary(self) -> str:
        parts = [f"{self.LABELS.get(k, k)} {v / 1000.0:.1f}s"
                 for k, v in self.ms.items()]
        return " · ".join(parts)


# 「受理提示必须先于结果」时序保护窗口（秒）。
#
# 回复接口是 202 异步受理，结果经 WS ``send_result`` 推送。前端在 HTTP
# 响应的 onSuccess 里显示「已加入发送队列」，WS 结果到达时覆盖它。但发送
# 线程可能在 submit() 返回后的几毫秒内就失败（微信关到托盘 = 0.0 秒
# fail-closed、微信未登录、_gui_lock 被占用等），而 notifier.broadcast()
# 又是 run_coroutine_threadsafe 投递到主事件循环，结果常常**比 threadpool
# 里的 202 响应更早**送达浏览器。后到的 onSuccess 就把失败提示覆盖成
# 「已加入发送队列」——用户看到的就是「只进队列、没有任何后续动作」
# （2026-11-15 18:26:26 实测）。
#
# 让发送线程取到任务后至少等这么久再开工，保证 HTTP 202 先落地；同时
# 前端也用 settled 标记兜住任何极端调度顺序（见 OpportunityDetail.tsx）。
ACK_LEAD_SEC = 0.35


@dataclass
class SendTask:
    """一条待发送任务。"""
    opportunity_id: int
    session_username: str              # 会话 wxid（校验/落库用）
    session_name: str                  # 显示名（open_chat 用）
    text: str
    at: List[str] = field(default_factory=list)   # @群成员昵称（仅群会话）
    quote_local_id: Optional[int] = None          # 引用回复的消息 local_id
    # 「引用最新一条消息」意图：执行发送时直接右键当时最新的一条，不用
    # 文本片段去列表里翻找。页面加载后群里进新消息（尤其是自己刚发的
    # 回复）会让固定 local_id 退化成「引用更早消息」，那条路径最脆弱。
    quote_latest: bool = False
    # 人工确认模式（用户反馈：怕发错人）：True = 只把内容填进微信输入框，
    # 由用户自己在微信里核对收件人后点发送；False = 自动发送并按数据库确认。
    manual_confirm: bool = False
    # 回复去向：session=发到商机所在会话（群商机=群内回复）；
    # sender=私信发给该条消息的发送人（此时 session_username/session_name
    # 已被调用方换成发送人，本字段只用于文案与落库）。
    target: str = "session"
    # submit() 写入的入队时刻（monotonic）。发送线程据此执行 ACK_LEAD_SEC
    # 时序保护；手工构造的测试任务保持 0，不引入任何等待。
    enqueued_mono: float = 0.0
    # submit() 盖章的监控账号（目录名）。发送前与当前监控账号比对：
    # 切换账号后旧账号的排队回复会被取消，防止发到新账号的同名联系人。
    account: str = ""


class Sender:
    """串行发送线程（应用生命周期内单例）。"""

    def __init__(self, database: Database, notifier: Notifier) -> None:
        self._db = database
        self._notifier = notifier
        self._queue: "queue.Queue[Optional[SendTask]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._prewarm_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._gui = None              # WeChatGUI 实例（仅本线程创建/使用）
        # GUI 操作互斥锁：发送线程与「跳转微信」API 线程共用。UIA 自动化
        # 全程共享一个微信窗口，两个流程并发操作会互相把对方的窗口状态
        # （当前会话、焦点、置顶）打乱，必须串行化。
        self._gui_lock = threading.Lock()
        self._message_lookup = None   # 注入：按 local_id 取消息
        self._latest_lookup = None    # 注入：取会话最新消息 local_id
        # 注入：当前监控账号 ↔ 微信实例绑定（双开时只操作该 PID 的窗口）
        self._binding_provider = None
        self._gui_account = ""        # 缓存 GUI 所属账号（切换账号时重建）
        self._last_gui_error = ""     # 最近一次建 GUI 失败的用户可读原因

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, prewarm: bool = False) -> None:
        """启动发送线程。

        Args:
            prewarm: 是否在后台预热微信 UIA 控件树。服务启动时传 True：
                微信的 UIA 树被回收后重新物化实测要 15~30 秒，这段耗时
                压在用户点「发送」上就是「微信到前台后半天没动静」。
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="kefu-sender", daemon=True)
        self._thread.start()
        logger.info("发送线程已启动")
        if prewarm:
            self._start_prewarm()

    def _start_prewarm(self) -> None:
        """后台预热微信控件树（不阻塞服务启动、不动微信前后台）。"""
        if self._prewarm_thread and self._prewarm_thread.is_alive():
            return
        self._prewarm_thread = threading.Thread(
            target=self._prewarm, name="kefu-sender-prewarm", daemon=True)
        self._prewarm_thread.start()

    def _prewarm(self) -> None:
        """常驻预热巡检线程：微信就绪后自动物化 UIA 控件树。

        服务启动时微信往往还没登录（甚至没启动），一次性预热只会失败一次
        就结束，用户登录后的首次发送仍要付出 15~30 秒的树物化耗时（实测
        「打开会话 25.4s」的主体就是它）。因此改为常驻巡检：每轮检查
        「微信活着且引擎未就绪」才做一次 warm_up；warm 失败（微信活着但
        树迟迟物化不了）按 15→30→60→120 秒退避，其余情况保持 15 秒轮询——
        微信重启（rebind 清空引擎缓存）后也会再次自动预热。

        预热全程持 ``_gui_lock``——微信的 UIA provider 是单线程的，两个
        线程同时 ``ControlFromHandle``/``Exists`` 会互相排队，曾把一次发送
        拖到 60 秒以上且全程无日志。巡检路径绝不广播状态事件（``quiet``），
        否则微信没启动时每 15 秒就会往前端刷一次「微信未启动」。
        """
        interval = 15.0
        while not self._stop.is_set():
            state = "failed"
            try:
                state = self._prewarm_once()
            except Exception:
                logger.debug("微信预热巡检异常（忽略）", exc_info=True)
            if state == "failed":
                interval = min(interval * 2, 120.0)
            else:
                interval = 15.0
            self._stop.wait(interval)

    def _prewarm_once(self) -> str:
        """单轮预热尝试（轻量、无广播）。

        Returns:
            ``"warmed"``  本次完成了预热（引擎就绪，首次发送可立即开始）；
            ``"ready"``   引擎本就就绪，无需预热；
            ``"no_wechat"`` 微信未启动/窗口未就绪，稍后重试；
            ``"failed"``  微信活着但预热失败（如树物化超时），调用方应退避。
        """
        try:
            from wechatauto.guia import WeChatGUI  # noqa: F401  探测依赖
        except Exception:                 # 依赖未装（如 linux CI）
            return "no_wechat"
        gui = self._ensure_gui(quiet=True)
        if gui is None:
            return "no_wechat"
        try:
            if not gui.is_alive():
                return "no_wechat"
            # 微信关到托盘（Qt hide）时控件树根本不会物化，warm_up 会白等
            # 30s 并反复写 gate byte；后台每 15s 巡检一次就能把 Qt 输入
            # 写挂（2026-11-15 11:34:57 实测：同 PID 连写 6 次后限流）。
            # 静默跳过、不打补丁，等用户点开窗口后下一轮再预热。
            tray = getattr(gui, "is_tray_hidden", None)
            if callable(tray) and tray():
                return "no_wechat"
            if getattr(gui, "_uia", None) is not None:
                return "ready"            # 引擎已就绪：零成本巡检
        except Exception:
            return "failed"
        if not self._gui_lock.acquire(timeout=0.5):
            return "failed"               # 发送正在占用窗口，稍后再试
        try:
            t0 = time.monotonic()
            ok = gui.warm_up(timeout=30.0)
            logger.info("微信预热%s（耗时 %.1fs）",
                        "完成（首次发送可立即开始）" if ok
                        else "未完成（微信就绪后会自动重试）",
                        time.monotonic() - t0)
            return "warmed" if ok else "failed"
        except Exception:
            logger.debug("微信预热异常（忽略）", exc_info=True)
            return "failed"
        finally:
            self._gui_lock.release()

    def stop(self) -> None:
        """停止发送线程。"""
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # pragma: no cover
            pass
        if self._thread:
            self._thread.join(timeout=10)
        self._thread = None
        logger.info("发送线程已停止")

    def submit(self, task: SendTask) -> bool:
        """任务入队（串行消费），同时打上入队时刻供时序保护使用。"""
        try:
            task.enqueued_mono = time.monotonic()
            # 盖章当前监控账号：发送前据此拦截「切换账号前创建的回复」，
            # 否则会用新账号的微信去打开同名会话，发给错误的人。
            if not getattr(task, "account", ""):
                task.account = self._binding_account()
            self._queue.put_nowait(task)
            return True
        except queue.Full:  # pragma: no cover - 无界队列实际不会满
            return False

    @property
    def pending(self) -> int:
        """排队中的任务数。"""
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _run(self) -> None:
        """串行消费：任何异常不得终止线程（共享约定 4）。"""
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                break
            # 极快失败（托盘态 0.0s）不能早于 HTTP 202 受理提示推给前端，
            # 否则失败结果会被后到的「已加入发送队列」覆盖成「没反应」。
            # 这一步也在保护伞内：任何异常都不得终止发送线程（共享约定 4）。
            try:
                if self._wait_ack_lead(task):
                    break                 # 停机：不再处理任何发送任务
            except Exception:
                logger.exception("发送时序保护异常（忽略，继续处理任务 #%d）",
                                 task.opportunity_id)
            if self._stop.is_set():
                break
            try:
                self._process(task)
            except Exception:
                logger.exception("处理发送任务异常（任务 #%d）", task.opportunity_id)
                self._fail(task, "发送失败：系统内部错误，请查看日志")

    def _wait_ack_lead(self, task: SendTask) -> bool:
        """给 HTTP 202 一点领先时间；返回 True 表示线程已收到停机信号。

        用 ``Event.wait`` 而不是 ``time.sleep``，stop() 时可立即退出、不会
        让 Ctrl+C 卡在这 0.35 秒上。
        """
        enqueued = float(getattr(task, "enqueued_mono", 0.0) or 0.0)
        if enqueued <= 0:
            return False                 # 手工构造的任务：不引入等待
        remain = ACK_LEAD_SEC - (time.monotonic() - enqueued)
        if remain <= 0:
            return False
        return bool(self._stop.wait(remain))

    # ------------------------------------------------------------------
    # 任务处理
    # ------------------------------------------------------------------
    def _process(self, task: SendTask) -> None:
        """执行一次完整发送（三重校验），并记录每段耗时。

        GUI 操作全程持 ``_gui_lock``：与「跳转微信」API 互斥，等待超时按
        发送失败处理（前端可重试），绝不与跳转流程并发操作微信窗口。
        """
        stage = _Stages()
        if not self._gui_lock.acquire(timeout=GUI_LOCK_TIMEOUT_SEC):
            self._fail(task, "微信窗口正被其他操作占用（可能正在跳转会话），"
                             "请稍后重试", stage)
            return
        try:
            self._process_locked(task, stage)
        finally:
            self._gui_lock.release()

    @staticmethod
    def _window_not_visible_message(gui) -> str:
        """窗口不可见的失败文案：区分「关到系统托盘」与「遮挡/锁屏」。

        托盘态下 ensure_visible() 会主动 fail-closed（不 ShowWindow、不写
        补丁），提示必须让用户知道该去点哪个图标，而不是笼统地说
        「请检查后重试」——用户看不到窗口时根本无从下手（2026-11-15
        11:33 用户反馈「界面不能动」）。
        """
        try:
            getter = getattr(gui, "last_open_error", None)
            reason = str(getter() or "") if callable(getter) else ""
            tray = getattr(gui, "is_tray_hidden", None)
            if reason == "tray_hidden" or (callable(tray) and tray()):
                return ("微信已关闭到系统托盘，自动点击托盘图标没能唤起主窗口："
                        "请手动点击任务栏右下角的微信图标打开主窗口，"
                        "等 1~2 秒后重试。为避免把微信界面卡死，系统不会"
                        "强行 SW_SHOW 托盘窗口，本次已取消发送")
        except Exception:
            pass
        return "微信窗口不可见（可能被遮挡或锁屏），请检查后重试"

    def _process_locked(self, task: SendTask, stage: _Stages) -> None:
        """发送主流程（调用方已持 ``_gui_lock``）。"""
        # 账号一致性校验必须在碰微信窗口之前：切换账号后旧账号残留的
        # 回复任务不能用新账号的微信去发（同名联系人会发错人）。
        binding = self._wechat_binding()
        current_account = (str(binding.get("account") or "")
                           if binding else "")
        task_account = str(getattr(task, "account", "") or "")
        if task_account and current_account and task_account != current_account:
            self._fail(
                task,
                f"该回复属于监控账号 {task_account}，当前已切换为 "
                f"{current_account}，已取消发送以免发错账号；请确认监控"
                "对象后重新回复", stage)
            return
        gui = self._ensure_gui()
        stage.mark("init")
        if gui is None:
            # _ensure_gui 只广播了全局 status_change，没有 send_result。
            # 前端等的是 send_result：不补这一条就会永远停在「发送中 /
            # 已加入发送队列」，看起来像系统没反应（与 0 秒失败被覆盖
            # 是同一类症状）。
            reason = str(getattr(self, "_last_gui_error", "") or "")
            self._fail(task, reason or (
                "未找到微信主窗口（微信可能未启动，或自动化组件不可用），"
                "请确认微信已登录后重试"), stage)
            return
        try:
            # 校验①：微信窗口可用性（登录态 + 可见性 + 窗口前置）
            if not gui.is_alive():
                self._fail(task, "微信未登录或窗口不存在，请检查后重试", stage)
                return
            # 前置只交给 ensure_visible()：它会置前并保持置顶，且已加
            # 「微信仍是前台」的缓存判定。旧代码这里先调一次
            # bring_to_front()（NOTOPMOST），ensure_visible() 又置顶一次，
            # 每次发送把窗口 Z 序翻两遍，是 Qt 控件树反复回收的诱因之一。
            if not gui.ensure_visible():
                self._fail(task, self._window_not_visible_message(gui), stage)
                return
            stage.mark("front")
            # 校验②：定位并打开目标会话（open_chat 内部有 _chat_open_confirmed）
            who = self._chat_name(task)
            openim = self._is_openim(task.session_username)
            if openim:
                # 企业微信联系人：按昵称搜索并强制选中「企业微信」那条，
                # 避免与同名普通好友混淆（曾把消息发给同名好友）
                opener = getattr(gui, "open_chat_prefer_openim", None)
                corp = self._corp_hint(task.session_name)
                if callable(opener):
                    try:
                        opened = bool(opener(who, corp))
                    except TypeError:   # 旧签名（只接 name）
                        opened = bool(opener(who))
                else:
                    opened = bool(gui.open_chat(who))
            else:
                opened = bool(gui.open_chat(who))
            if not opened:
                reason = ""
                try:
                    getter = getattr(gui, "last_open_error", None)
                    if callable(getter):
                        reason = str(getter() or "")
                except Exception:
                    reason = ""
                if reason == "uia_unavailable":
                    # 窗口被关闭到托盘/最小化时 Qt 不物化控件树。发送链路
                    # 已按用户要求禁用截图/OCR 兜底，直接给出可操作提示，
                    # 不再让用户白等 30 秒唤醒 + 20 秒 OCR。
                    self._fail(
                        task,
                        "微信控件树不可用（窗口可能被关闭到托盘或最小化），"
                        "已取消发送；请点开微信窗口、等 1~2 秒后重试。"
                        "为避免发错会话，本次未做任何截图兜底操作", stage)
                    return
                if reason == "chat_mismatch":
                    self._fail(
                        task,
                        "微信当前打开的会话与目标不一致，已取消发送以免发错人；"
                        "请在微信里手动打开目标会话后重试", stage)
                    return
                if reason == "tree_not_ready":
                    # 微信 UIA 控件树被回收后重新物化实测要 15~30 秒，这期间
                    # 搜索框根本找不到。不能说「无法定位会话」——会话其实在。
                    self._fail(
                        task,
                        "微信控件树正在唤醒（首次约 15~30 秒），请稍后重试；"
                        "若反复出现请确认微信窗口未被最小化", stage)
                    return
                if reason == "tree_throttled":
                    # 限流生效时旧提示只说「无法定位会话」，用户看不到
                    # 「该重启微信了」，会一直重试并白等 60~80 秒。
                    self._fail(
                        task,
                        "微信控件树反复回收，自动修复已达上限；请重启一次微信，"
                        "并避免发送过程中最小化微信窗口或频繁切换会话，再重试",
                        stage)
                    return
                if openim and reason == "identity_mismatch":
                    self._fail(
                        task,
                        f"无法定位企业微信会话：{task.session_name}"
                        "（微信搜索里同名的那几个都不是这家企业的联系人——"
                        "只有同名普通好友；已取消发送以免发错人。可先在微信里"
                        "手动打开该企业微信会话再重试，当前会话已经是它时会"
                        "直接复用）",
                        stage)
                    return
                if openim and reason == "corp_mismatch":
                    self._fail(
                        task,
                        f"无法定位会话：{task.session_name}"
                        "（打开的同名联系人不属于该企业微信，已取消发送）",
                        stage)
                    return
                hint = ""
                if openim:
                    hint = ("（未找到该企业微信会话；若微信里存在同名普通"
                            "好友，不排除被误开，已取消发送以免发错人）")
                self._fail(task, f"无法定位会话：{task.session_name}{hint}",
                           stage)
                return
            # 会话已确认打开：后续输入/引用/发送都不必再重复「搜索打开」
            time.sleep(0.5)  # 等会话面板渲染稳定（与 wechatauto 内部节奏一致）
            stage.mark("open")
            # 人工确认模式：只填进输入框，由用户在微信里点发送
            if task.manual_confirm:
                self._fill_only(gui, task, who, stage)
                return
            # 校验③：发送 + 数据库回读确认（verify=True）
            try:
                resp = self._do_send(gui, task, who)
            except QuoteTargetUnavailable as exc:
                self._fail(task, str(exc), stage)
                return
            stage.mark("send")
            message = str(resp.get("message") or "") if isinstance(resp, dict) else ""
            ok = bool(getattr(resp, "is_success", False))
            # 「已操作发送但数据库未确认」：UI 层发送已完成（输入框回车成功），
            # 只是数据库回读超时——微信重启后消息分片重建首分钟很慢，8s 轮询
            # 不够。按已回复处理：用户看到失败去重发就是重复消息（实测隐患）。
            #
            # 2026-11-15 收紧：窗口从托盘/最小化恢复后，旧实现可能把消息
            # 打进「另一个会话」，DB 回读自然查不到，却仍被当成成功标记
            # replied（用户实测事故）。现在只有当组件层明确给出
            # ``chat_verified=True``（发送前用 UIA 实时校验过会话）才允许
            # 按已回复处理；否则一律判失败，绝不误标。
            payload_data = {}
            if isinstance(resp, dict):
                val = resp.get("data")
                if isinstance(val, dict):
                    payload_data = val
            verified = bool(payload_data.get("chat_verified"))
            # 组件层对不同接口用了不同前缀（消息 / 引用 / @消息 / 文件 /
            # 图片 / 回复「已操作发送，但数据库未确认」）。旧代码只认
            # 「消息已操作发送」一种，引用回复 DB 未确认时会被当成真失败 →
            # 用户看到失败去重发 → 重复消息。统一按两个关键词识别，且
            # 仍严格要求发送前实时校验过会话（chat_verified=True）。
            db_unconfirmed = ("已操作发送" in message
                              and "数据库未确认" in message)
            unconfirmed = not ok and db_unconfirmed and verified
            if ok or unconfirmed:
                self._db.set_opportunity_replied(
                    task.opportunity_id, task.text, target=task.target)
                self._notifier.broadcast("send_result", {
                    "opportunity_id": task.opportunity_id,
                    "ok": True,
                    "target": task.target,
                    "message": message if unconfirmed
                    else (message or "已发送并经数据库确认"),
                    "unconfirmed": unconfirmed,
                    "stages": stage.ms,
                    "total_ms": stage.total_ms(),
                })
                self._notifier.broadcast("opportunity_update", {
                    "opportunity_id": task.opportunity_id,
                    "status": "replied",
                })
                logger.info("[发送完成%s] 商机 #%d → %s%s | %s | 合计 %.1fs",
                            "·DB未确认" if unconfirmed else "",
                            task.opportunity_id, task.session_name,
                            "（私信）" if task.target == "sender" else "",
                            stage.summary(), stage.total_ms() / 1000.0)
            else:
                if db_unconfirmed and not verified:
                    # 会话没实时校验过：消息可能发到了别的会话，绝不能让
                    # 用户以为只是「DB 慢」而放任不管（2026-11-15 事故）。
                    message = (
                        "微信可能已把消息发到了别的会话（发送前未能确认目标"
                        "会话），数据库回读未确认；已阻止标记为已回复。"
                        "请先在微信里核对该消息发给了谁，确认无误后手动处理"
                        "该商机，不要直接重发")
                self._fail(task, f"发送失败：{message or '未知原因，请重试'}", stage)
        except Exception as exc:
            logger.exception("发送任务执行异常（商机 #%d）", task.opportunity_id)
            self._fail(task, f"发送失败：{exc}", stage)

    # ------------------------------------------------------------------
    # 会话名 / 企业微信身份
    # ------------------------------------------------------------------
    @staticmethod
    def _is_openim(username: str) -> bool:
        """会话是否企业微信/客服账号（@openim / @kefu）。"""
        u = (username or "").strip().lower()
        return u.endswith("@openim") or "@kefu" in u

    @staticmethod
    def _corp_hint(session_name: str) -> str:
        """「昵称@企业名」里的企业名（非企业微信显示名返回空串）。"""
        name = str(session_name or "")
        if "@" not in name:
            return ""
        return name.split("@", 1)[1].strip()

    @classmethod
    def _chat_name(cls, task: SendTask) -> str:
        """open_chat 用的搜索关键词。

        企业微信联系人的显示名按微信习惯拼成「昵称@企业简称」，但这个
        后缀**不是**微信搜索框能命中的文本（搜「安佳胤@我的车车」只会
        搜出空结果，白等一轮重试），所以搜索时只取 @ 之前的昵称，身份
        由 :meth:`open_chat_prefer_openim` 在结果里区分。
        """
        name = str(task.session_name or "").strip() or task.session_username
        if cls._is_openim(task.session_username) and "@" in name:
            return name.split("@", 1)[0].strip() or name
        return name

    def _fill_only(self, gui, task: SendTask, who: str, stage: _Stages) -> None:
        """人工确认模式：把内容填进输入框即结束（不按发送键）。

        不写 ``replied`` 状态、不置「已回复」——消息还没发出去，状态必须
        保持待处理，否则用户会以为已经回过了（漏回复比多发一次更常见）。
        """
        filler = getattr(gui, "input_only", None)
        if not callable(filler):
            self._fail(task, "当前微信自动化组件不支持「只填入不发送」，"
                             "请改用自动发送或手动跳转到微信", stage)
            return
        try:
            resp = filler(task.text, who=who,
                          who_username=task.session_username)
        except Exception as exc:
            logger.exception("填入输入框异常（商机 #%d）", task.opportunity_id)
            self._fail(task, f"填入输入框失败：{exc}", stage)
            return
        stage.mark("send")
        ok = bool(getattr(resp, "is_success", False))
        message = str(resp.get("message") or "") if isinstance(resp, dict) else ""
        if not ok:
            self._fail(task, f"填入输入框失败：{message or '未知原因'}", stage)
            return
        self._notifier.broadcast("send_result", {
            "opportunity_id": task.opportunity_id,
            "ok": True,
            "manual": True,
            "target": task.target,
            "message": message or (
                f"已填入{'私信' if task.target == 'sender' else '微信'}"
                f"输入框（{task.session_name}），请在微信里核对后点发送"),
            "stages": stage.ms,
            "total_ms": stage.total_ms(),
        })
        logger.info("[已填入输入框] 商机 #%d → %s%s（人工确认模式）| %s",
                    task.opportunity_id, task.session_name,
                    "（私信）" if task.target == "sender" else "",
                    stage.summary())

    def _do_send(self, gui, task: SendTask, who: Optional[str] = None):
        """按 @成员 / 引用 / 普通文本选择发送接口（全部 verify=True 回读确认）。

        who_username 传会话 wxid：数据库回读直查，跳过显示名模糊搜索
        （既慢，同名联系人时还可能确认失败导致白等 8s 轮询）。
        UIA 会话比较仍用显示名（who），两者互不影响。

        Args:
            who: 搜索/校验用的会话名（与企业微信账号已剥离 @企业名 后缀）。
        """
        chat = who or self._chat_name(task)
        want_quote = bool(task.quote_local_id or task.quote_latest)
        if task.at:
            members = [m.strip() for m in task.at if m.strip()]
            # 组合回复（同时选了 @发送人 + 引用最新一条）必须走同一条草稿：
            # 旧实现是 if/return 互斥，@ 分支先返回 → 引用被静默丢弃但接口
            # 仍报成功（2026-11-15 实测）。这里把引用意图一并交给 at_members。
            # 必须用 callable：旧组件/测试桩可能把 at_members 定义成 None，
            # hasattr 仍为 True，直接调用会抛 TypeError（组合分支实测踩坑）。
            at_batch = getattr(gui, "at_members", None)
            if callable(at_batch):
                if want_quote:
                    target_text = self._quote_target_text(task)
                    logger.info("组合回复：@%s + 引用（target=%s）",
                                "、".join(members) or "成员",
                                "最新一条" if target_text is None else target_text[:20])
                    return at_batch(members, task.text,
                                    who=chat, verify=True,
                                    who_username=task.session_username,
                                    quote=True,
                                    quote_target_text=target_text)
                return at_batch(members, task.text,
                                who=chat, verify=True,
                                who_username=task.session_username)
            # 旧组件没有 at_members：逐个 @ 兜底，此时无法安全叠加引用，
            # 明确失败好过静默只发 @。
            if want_quote:
                from wechatauto.param import WxResponse
                return WxResponse.failure(
                    "当前微信自动化组件不支持「引用 + @」组合发送，请升级组件")
            resp = None
            for i, member in enumerate(members):
                text = task.text if i == len(members) - 1 else ""
                resp = gui.at_member(member, text, who=chat,
                                     verify=True,
                                     who_username=task.session_username)
            return resp
        if want_quote:
            target_text = self._quote_target_text(task)
            return gui.quote_msg(task.text, who=chat,
                                 target_text=target_text, verify=True,
                                 assume_open=True,
                                 who_username=task.session_username)
        return gui.send_msg(task.text, who=chat, verify=True,
                            assume_open=True,
                            who_username=task.session_username)

    @staticmethod
    def _quote_snippet(content: str) -> str:
        """把消息原文压成可用于定位消息行的短文本片段。

        清洗规则：去掉群消息的「wxid:」/「显示名:」前缀、去掉 XML 标签
        （小程序/链接消息正文是 XML，直接拿去匹配永远匹配不到）、折成
        单行并截断。返回空串表示「这条消息没有可识别文字」。
        """
        text = str(content or "").strip()
        if not text:
            return ""
        # 群消息正文常带「wxid_xxx:」或「昵称:」首行前缀
        text = re.sub(r"^[^\s:：\n]{1,64}[:：]\s*\n", "", text)
        # 同行的 wxid 前缀（如「wxid_abc: 明天发货」）也要去掉，否则拿去
        # 匹配消息行永远匹配不到（微信列表里不显示这个前缀）
        text = re.sub(r"^(?:wxid_[0-9A-Za-z_-]+|gh_[0-9A-Za-z]+"
                      r"|[0-9A-Za-z_-]+@chatroom)[:：]\s*", "", text)
        # 小程序/链接/引用等 XML 正文：取 <title> 最有辨识度
        if "<" in text and ">" in text:
            m = re.search(r"<title>(.*?)</title>", text, re.S)
            text = m.group(1) if m else re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = " ".join(text.split())
        if not text:
            return ""
        if _PLACEHOLDER_RE.match(text):
            return ""      # [图片]/[小程序] 之类没有真实文字可定位
        return text[:QUOTE_SNIPPET_MAX]

    def _quote_target_text(self, task: SendTask) -> Optional[str]:
        """引用回复：定位被引用消息。

        - ``quote_latest``（用户点了「引用最新一条消息」）→ 恒返回 None，
          执行时直接右键当时的最新一条；不受页面加载后新消息影响；
        - 目标就是该会话最新一条 → 返回 None，直接引用最新消息（不依赖
          文本匹配，图片/小程序/链接消息也能引用）；
        - 更早的消息 → 返回清洗后的文本片段供 UIA 在消息列表里定位；
        - 更早且无可识别文字 → 抛 QuoteTargetUnavailable，提示用户改用
          「引用最新一条」，绝不默默引用错消息。
        """
        if task.quote_latest:
            logger.debug("引用回复：latest 意图 → 执行时直接右键最新一条")
            return None
        if not task.quote_local_id:
            return None
        msg = None
        try:
            msg = self._lookup_message(task.session_username,
                                       task.quote_local_id)
        except Exception:
            msg = None
        latest = self._lookup_latest(task.session_username)
        if latest and latest == task.quote_local_id:
            return None
        snippet = self._quote_snippet((msg or {}).get("content", ""))
        # 走「历史文本定位」是故障高发路径，日志里必须一眼能认出来：
        # 页面加载后群里进了新消息（含自己刚发的回复）就会退化到这里。
        logger.info("引用回复目标已非最新（quote=%s latest=%s）→ 按文本定位：%s",
                    task.quote_local_id, latest, snippet[:30])
        if not snippet:
            raise QuoteTargetUnavailable(
                "这条消息没有可识别的文字（图片/小程序/文件等），无法在微信"
                "消息列表里精确定位；请改为「引用最新一条消息」或回复纯文本")
        return snippet

    def _lookup_message(self, username: str, local_id: int) -> Optional[dict]:
        """借助 wechat_service 查消息（避免循环导入，运行时注入）。"""
        lookup = getattr(self, "_message_lookup", None)
        if lookup is None:
            return None
        return lookup(username, local_id)

    def _lookup_latest(self, username: str) -> int:
        """会话最新一条消息的 local_id（0 = 未知）。"""
        lookup = getattr(self, "_latest_lookup", None)
        if lookup is None:
            return 0
        try:
            return int(lookup(username) or 0)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # 跳转微信（POST /api/wechat/jump）：调起微信并打开指定会话输入窗口
    # ------------------------------------------------------------------
    def jump_to_session(self, session_username: str,
                        session_name: str) -> tuple[bool, str]:
        """把微信带到前台并打开指定会话（不发送任何消息）。

        与发送线程共用 ``_gui_lock``：正在发送时这里同步等待（最长
        :data:`GUI_LOCK_TIMEOUT_SEC` 秒），避免两个流程并发操作窗口。

        Args:
            session_username: 会话 wxid（预留：与发送链路同源校验用）。
            session_name: 会话显示名（open_chat 是 UIA 文本匹配，必须传）。

        Returns:
            (是否成功, 用户可读中文提示)。**绝不抛异常**：HTTP 层直接把
            提示回给前端弹 toast，失败也不能变成 500。
        """
        name = str(session_name or "").strip()
        if not name:
            return False, "缺少会话名称，无法跳转"
        # 企业微信联系人的显示名带「@企业名」后缀，不是搜索框能命中的文本；
        # 跳转同样按昵称搜索，并优先选中企业微信那条（同名好友不会串）
        keyword = name
        openim = self._is_openim(session_username)
        if openim and "@" in keyword:
            keyword = keyword.split("@", 1)[0].strip() or keyword
        if not self._gui_lock.acquire(timeout=GUI_LOCK_TIMEOUT_SEC):
            return False, ("微信窗口正被其他操作占用（可能正在发送消息），"
                           "请稍后再试")
        try:
            gui = self._ensure_gui()
            if gui is None:
                return False, (str(getattr(self, "_last_gui_error", "") or "")
                               or "微信自动化组件不可用，无法跳转")
            # 与 _process_locked 同款三步：存活 → 前置可见 → 打开会话
            if not gui.is_alive():
                return False, "微信未登录或窗口不存在，请检查后重试"
            # 前置只交给 ensure_visible()：它会置前并保持置顶，且已加
            # 「微信仍是前台」的缓存判定。旧代码这里先调一次
            # bring_to_front()（NOTOPMOST），ensure_visible() 又置顶一次，
            # 每次发送把窗口 Z 序翻两遍，是 Qt 控件树反复回收的诱因之一。
            if not gui.ensure_visible():
                return False, self._window_not_visible_message(gui)
            if openim:
                opener = getattr(gui, "open_chat_prefer_openim", None)
                corp = self._corp_hint(name)
                if callable(opener):
                    try:
                        opened = bool(opener(keyword, corp))
                    except TypeError:
                        opened = bool(opener(keyword))
                else:
                    opened = bool(gui.open_chat(keyword))
            else:
                opened = bool(gui.open_chat(keyword))
            if not opened:
                return False, (f"无法定位会话：{name}，"
                               "请确认微信会话列表中存在该会话")
            logger.info("跳转微信完成：已打开会话 %s（username=%s）",
                        name, session_username)
            return True, f"已在微信中打开会话：{name}"
        except Exception as exc:  # UIA 偶发异常：结构化返回，不 500
            logger.exception("跳转微信异常（%s）", name)
            return False, f"跳转微信失败：{exc}"
        finally:
            self._gui_lock.release()

    # ------------------------------------------------------------------
    # GUI 实例
    # ------------------------------------------------------------------
    def _ensure_gui(self, quiet: bool = False):
        """线程内懒创建 WeChatGUI 单例；失败广播中文错误并返回 None。

        Args:
            quiet: True 时不广播状态事件、只记日志。预热巡检线程用它——
                巡检每 15 秒跑一次，微信没启动时若每次都广播，前端会被
                「微信未启动」提示刷屏；广播只留给真正的发送/跳转路径。

        缓存实例的窗口句柄失效（微信重启/重登）时先调 ``rebind()`` 重新
        定位：重绑成功直接复用，失败才丢弃重建。没有这一步，微信重启一次
        之后所有发送/跳转都会 0 秒报「微信未登录或窗口不存在」（实测：
        is_alive 只查缓存句柄 IsWindow，旧句柄已死恒 False）。
        """
        binding = self._wechat_binding()
        want_pid = 0
        want_account = ""
        if binding is not None:
            want_pid = int(binding.get("pid") or 0)
            want_account = str(binding.get("account") or "")
            instance_count = int(binding.get("instance_count") or 0)
            # 双开且无法确认归属；或用户指定账号但当前微信不是它
            unresolved = (not want_pid and (
                instance_count > 1
                or (bool(binding.get("pinned")) and instance_count > 0)))
            if unresolved:
                # 宁可不发，也不猜一个窗口
                self._gui = None
                self._gui_account = ""
                reason = str(binding.get("reason") or (
                    "检测到多个微信实例，无法确认当前监控账号对应的微信；"
                    "已暂停自动回复以免发到其它账号"))
                self._last_gui_error = reason
                if quiet:
                    logger.debug("账号实例未绑定（巡检静默）：%s", reason)
                else:
                    logger.warning("发送已阻止：%s", reason)
                    self._notifier.broadcast("status_change", {
                        "send_available": False, "message": reason})
                return None
        if self._gui is not None:
            gui_pid = int(getattr(self._gui, "pid", 0) or 0)
            gui_account = getattr(self, "_gui_account", "")
            switched = (
                (want_pid and gui_pid and gui_pid != want_pid)
                or (want_account and gui_account
                    and want_account != gui_account))
            if switched:
                logger.info("监控微信账号/实例已变化（%s/PID=%s → %s/PID=%s），"
                            "重建 GUI 实例", gui_account or "未知", gui_pid,
                            want_account or "自动", want_pid)
                self._gui = None
                self._gui_account = ""
        if self._gui is not None:
            if self._gui.is_alive():
                return self._gui
            try:
                if self._gui.rebind():
                    logger.info("微信窗口句柄已失效，重绑成功：hwnd=%s",
                                self._gui.main_hwnd)
                    return self._gui
            except Exception as exc:
                logger.warning("微信窗口重绑异常：%s", exc)
            logger.info("微信窗口重绑失败，重建 GUI 组件")
            self._gui = None
        try:
            from wechatauto.guia import WeChatGUI
        except Exception as exc:
            logger.error("微信自动化组件加载失败：%s", exc)
            self._gui = None
            self._gui_account = ""
            self._last_gui_error = f"微信自动化组件不可用（{exc}）"
            if not quiet:
                # 无具体任务上下文，广播全局状态事件
                self._notifier.broadcast("status_change", {
                    "send_available": False,
                    "message": f"微信自动化组件不可用（{exc}），无法发送消息",
                })
            return None
        try:
            if want_pid:
                try:
                    # 双开：只绑定监控账号对应的 Weixin.exe 实例
                    self._gui = WeChatGUI(pid=want_pid)
                except TypeError:
                    # 兼容不支持定向参数的替身实现
                    self._gui = WeChatGUI()
            else:
                self._gui = WeChatGUI()
            self._gui_account = want_account
            self._last_gui_error = ""
        except Exception as exc:
            self._gui = None
            self._gui_account = ""
            if want_pid:
                reason = (f"未找到监控微信的窗口（账号 "
                          f"{want_account or '未知'}，PID {want_pid}）；"
                          "请确认该微信已登录且窗口未被关闭")
            else:
                reason = ("未找到微信主窗口（微信可能未启动），"
                          "请确认微信已登录后重试")
            self._last_gui_error = reason
            if quiet:
                logger.debug("微信窗口定位失败（巡检静默）：%s", exc)
            else:
                logger.error("微信窗口定位失败：%s（%s）", exc, reason)
                self._notifier.broadcast("status_change", {
                    "send_available": False,
                    "message": f"{reason}，无法发送消息",
                })
            return None
        return self._gui

    # ------------------------------------------------------------------
    # 失败路径
    # ------------------------------------------------------------------
    def _fail(self, task: SendTask, message: str,
              stage: Optional[_Stages] = None) -> None:
        """广播发送失败事件（HTTP 层已 202 受理，结果经 WS 送达）。"""
        extra = ""
        if stage is not None:
            stage.mark("fail")
            extra = f" | {stage.summary()} | 合计 {stage.total_ms() / 1000.0:.1f}s"
        logger.warning("发送失败（商机 #%d）：%s%s", task.opportunity_id,
                       message, extra)
        payload = {
            "opportunity_id": task.opportunity_id,
            "ok": False,
            "message": message,
        }
        if stage is not None:
            payload["stages"] = stage.ms
            payload["total_ms"] = stage.total_ms()
        self._notifier.broadcast("send_result", payload)

    def set_message_lookup(self, lookup) -> None:
        """注入消息查询函数（main.py 组装时调用，避免循环导入）。"""
        self._message_lookup = lookup

    def set_latest_lookup(self, lookup) -> None:
        """注入「会话最新消息 local_id」查询函数（引用定位判定用）。"""
        self._latest_lookup = lookup

    def set_wechat_binding_provider(self, provider) -> None:
        """注入「监控账号 ↔ 微信实例」绑定查询（main.py 组装时调用）。"""
        self._binding_provider = provider

    def _wechat_binding(self) -> Optional[dict]:
        """读取当前账号绑定；未注入/查询失败时返回 None（保持旧行为）。"""
        provider = getattr(self, "_binding_provider", None)
        if provider is None:
            return None
        try:
            data = provider()
        except Exception:
            logger.debug("读取微信账号绑定失败（按未绑定处理）", exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def _binding_account(self) -> str:
        """当前监控账号目录名（未知时为空串，不做拦截）。"""
        binding = self._wechat_binding()
        return str(binding.get("account") or "") if binding else ""

# -*- coding: utf-8 -*-
"""GUI 发送服务：独立线程串行消费发送队列。

流程（技术方案 4.2）：
    submit(SendTask) → SenderThread 取任务（同一时刻仅一个 GUI 操作）
    → WeChatGUI 单例（线程内创建）→ is_alive / bring_to_front / ensure_visible
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


@dataclass
class SendTask:
    """一条待发送任务。"""
    opportunity_id: int
    session_username: str              # 会话 wxid（校验/落库用）
    session_name: str                  # 显示名（open_chat 用）
    text: str
    at: List[str] = field(default_factory=list)   # @群成员昵称（仅群会话）
    quote_local_id: Optional[int] = None          # 引用回复的消息 local_id


class Sender:
    """串行发送线程（应用生命周期内单例）。"""

    def __init__(self, database: Database, notifier: Notifier) -> None:
        self._db = database
        self._notifier = notifier
        self._queue: "queue.Queue[Optional[SendTask]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._gui = None              # WeChatGUI 实例（仅本线程创建/使用）
        # GUI 操作互斥锁：发送线程与「跳转微信」API 线程共用。UIA 自动化
        # 全程共享一个微信窗口，两个流程并发操作会互相把对方的窗口状态
        # （当前会话、焦点、置顶）打乱，必须串行化。
        self._gui_lock = threading.Lock()
        self._message_lookup = None   # 注入：按 local_id 取消息
        self._latest_lookup = None    # 注入：取会话最新消息 local_id

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动发送线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="kefu-sender", daemon=True)
        self._thread.start()
        logger.info("发送线程已启动")

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
        """任务入队（串行消费）。"""
        try:
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
            try:
                self._process(task)
            except Exception:
                logger.exception("处理发送任务异常（任务 #%d）", task.opportunity_id)
                self._fail(task, "发送失败：系统内部错误，请查看日志")

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

    def _process_locked(self, task: SendTask, stage: _Stages) -> None:
        """发送主流程（调用方已持 ``_gui_lock``）。"""
        gui = self._ensure_gui()
        stage.mark("init")
        if gui is None:
            return  # _ensure_gui 内已广播失败
        try:
            # 校验①：微信窗口可用性（登录态 + 可见性 + 窗口前置）
            if not gui.is_alive():
                self._fail(task, "微信未登录或窗口不存在，请检查后重试", stage)
                return
            try:
                gui.bring_to_front()
            except Exception:
                logger.debug("bring_to_front 异常（继续尝试 ensure_visible）")
            if not gui.ensure_visible():
                self._fail(task, "微信窗口不可见（可能被遮挡或锁屏），请检查后重试",
                           stage)
                return
            stage.mark("front")
            # 校验②：定位并打开目标会话（open_chat 内部有 _chat_open_confirmed）
            if not gui.open_chat(task.session_name):
                self._fail(task, f"无法定位会话：{task.session_name}，"
                                 "请确认微信会话列表中存在该会话", stage)
                return
            # 会话已确认打开：后续输入/引用/发送都不必再重复「搜索打开」
            time.sleep(0.5)  # 等会话面板渲染稳定（与 wechatauto 内部节奏一致）
            stage.mark("open")
            # 校验③：发送 + 数据库回读确认（verify=True）
            try:
                resp = self._do_send(gui, task)
            except QuoteTargetUnavailable as exc:
                self._fail(task, str(exc), stage)
                return
            stage.mark("send")
            message = str(resp.get("message") or "") if isinstance(resp, dict) else ""
            ok = bool(getattr(resp, "is_success", False))
            if ok:
                self._db.set_opportunity_replied(task.opportunity_id, task.text)
                self._notifier.broadcast("send_result", {
                    "opportunity_id": task.opportunity_id,
                    "ok": True,
                    "message": message or "已发送并经数据库确认",
                    "stages": stage.ms,
                    "total_ms": stage.total_ms(),
                })
                self._notifier.broadcast("opportunity_update", {
                    "opportunity_id": task.opportunity_id,
                    "status": "replied",
                })
                logger.info("[发送完成] 商机 #%d → %s | %s | 合计 %.1fs",
                            task.opportunity_id, task.session_name,
                            stage.summary(), stage.total_ms() / 1000.0)
            else:
                self._fail(task, f"发送失败：{message or '未知原因，请重试'}", stage)
        except Exception as exc:
            logger.exception("发送任务执行异常（商机 #%d）", task.opportunity_id)
            self._fail(task, f"发送失败：{exc}", stage)

    def _do_send(self, gui, task: SendTask):
        """按 @成员 / 引用 / 普通文本选择发送接口（全部 verify=True 回读确认）。

        who_username 传会话 wxid：数据库回读直查，跳过显示名模糊搜索
        （既慢，同名联系人时还可能确认失败导致白等 8s 轮询）。
        UIA 会话比较仍用显示名（who），两者互不影响。
        """
        if task.at:
            members = [m.strip() for m in task.at if m.strip()]
            # 新版一次 @ 多个成员（同一条消息）；旧版按「逐个 @」兜底
            if hasattr(gui, "at_members"):
                return gui.at_members(members, task.text,
                                      who=task.session_name, verify=True,
                                      who_username=task.session_username)
            resp = None
            for i, member in enumerate(members):
                text = task.text if i == len(members) - 1 else ""
                resp = gui.at_member(member, text, who=task.session_name,
                                     verify=True,
                                     who_username=task.session_username)
            return resp
        if task.quote_local_id:
            target_text = self._quote_target_text(task)
            return gui.quote_msg(task.text, who=task.session_name,
                                 target_text=target_text, verify=True,
                                 assume_open=True,
                                 who_username=task.session_username)
        return gui.send_msg(task.text, who=task.session_name, verify=True,
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

        - 目标就是该会话最新一条 → 返回 None，直接引用最新消息（不依赖
          文本匹配，图片/小程序/链接消息也能引用）；
        - 更早的消息 → 返回清洗后的文本片段供 UIA 在消息列表里定位；
        - 更早且无可识别文字 → 抛 QuoteTargetUnavailable，提示用户改用
          「引用最新一条」，绝不默默引用错消息。
        """
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
        if not self._gui_lock.acquire(timeout=GUI_LOCK_TIMEOUT_SEC):
            return False, ("微信窗口正被其他操作占用（可能正在发送消息），"
                           "请稍后再试")
        try:
            gui = self._ensure_gui()
            if gui is None:
                return False, "微信自动化组件不可用，无法跳转"
            # 与 _process_locked 同款三步：存活 → 前置可见 → 打开会话
            if not gui.is_alive():
                return False, "微信未登录或窗口不存在，请检查后重试"
            try:
                gui.bring_to_front()
            except Exception:
                logger.debug("bring_to_front 异常（继续尝试 ensure_visible）")
            if not gui.ensure_visible():
                return False, "微信窗口不可见（可能被遮挡或锁屏），请检查后重试"
            if not gui.open_chat(name):
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
    def _ensure_gui(self):
        """线程内懒创建 WeChatGUI 单例；失败广播中文错误并返回 None。"""
        if self._gui is not None:
            return self._gui
        try:
            from wechatauto.guia import WeChatGUI
        except Exception as exc:
            logger.error("微信自动化组件加载失败：%s", exc)
            self._gui = None
            # 无具体任务上下文，广播全局状态事件
            self._notifier.broadcast("status_change", {
                "send_available": False,
                "message": f"微信自动化组件不可用（{exc}），无法发送消息",
            })
            return None
        try:
            self._gui = WeChatGUI()
        except Exception as exc:
            logger.error("微信窗口定位失败：%s", exc)
            self._gui = None
            self._notifier.broadcast("status_change", {
                "send_available": False,
                "message": f"未找到微信主窗口（微信可能未启动），无法发送消息",
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

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

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from app.db import Database
from app.notifier import Notifier

logger = logging.getLogger(__name__)


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
        """执行一次完整发送（三重校验）。"""
        gui = self._ensure_gui()
        if gui is None:
            return  # _ensure_gui 内已广播失败
        try:
            # 校验①：微信窗口可用性（登录态 + 可见性 + 窗口前置）
            if not gui.is_alive():
                self._fail(task, "微信未登录或窗口不存在，请检查后重试")
                return
            try:
                gui.bring_to_front()
            except Exception:
                logger.debug("bring_to_front 异常（继续尝试 ensure_visible）")
            if not gui.ensure_visible():
                self._fail(task, "微信窗口不可见（可能被遮挡或锁屏），请检查后重试")
                return
            # 校验②：定位并打开目标会话（open_chat 内部有 _chat_open_confirmed）
            if not gui.open_chat(task.session_name):
                self._fail(task, f"无法定位会话：{task.session_name}，"
                                 "请确认微信会话列表中存在该会话")
                return
            import time as _time
            _time.sleep(0.8)  # 等会话面板渲染稳定（与 wechatauto 内部节奏一致）
            # 校验③：发送 + 数据库回读确认（verify=True）
            resp = self._do_send(gui, task)
            message = str(resp.get("message") or "") if isinstance(resp, dict) else ""
            ok = bool(getattr(resp, "is_success", False))
            if ok:
                self._db.set_opportunity_replied(task.opportunity_id, task.text)
                self._notifier.broadcast("send_result", {
                    "opportunity_id": task.opportunity_id,
                    "ok": True,
                    "message": message or "已发送并经数据库确认",
                })
                self._notifier.broadcast("opportunity_update", {
                    "opportunity_id": task.opportunity_id,
                    "status": "replied",
                })
                logger.info("商机 #%d 回复发送成功", task.opportunity_id)
            else:
                self._fail(task, f"发送失败：{message or '未知原因，请重试'}")
        except Exception as exc:
            logger.exception("发送任务执行异常（商机 #%d）", task.opportunity_id)
            self._fail(task, f"发送失败：{exc}")

    def _do_send(self, gui, task: SendTask):
        """按 @成员 / 引用 / 普通文本选择发送接口（全部 verify=True 回读确认）。"""
        if task.at:
            # 多个 @ 成员：先逐个 @（空正文），最后一个携带正文一次发送
            members = [m.strip() for m in task.at if m.strip()]
            resp = None
            for i, member in enumerate(members):
                text = task.text if i == len(members) - 1 else ""
                resp = gui.at_member(member, text, who=task.session_name,
                                     verify=True)
            return resp
        if task.quote_local_id:
            target_text = self._quote_target_text(task)
            return gui.quote_msg(task.text, who=task.session_name,
                                 target_text=target_text, verify=True)
        return gui.send_msg(task.text, who=task.session_name, verify=True)

    def _quote_target_text(self, task: SendTask) -> Optional[str]:
        """引用回复：按 local_id 取被引用消息内容片段供 OCR 定位。"""
        if not task.quote_local_id:
            return None
        try:
            msg = self._lookup_message(task.session_username,
                                       task.quote_local_id)
        except Exception:
            msg = None
        if msg and msg.get("content"):
            return str(msg["content"])[:40]
        return None  # 省略时 quote_msg 引用最近一条

    def _lookup_message(self, username: str, local_id: int) -> Optional[dict]:
        """借助 wechat_service 查消息（避免循环导入，运行时注入）。"""
        lookup = getattr(self, "_message_lookup", None)
        if lookup is None:
            return None
        return lookup(username, local_id)

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
    def _fail(self, task: SendTask, message: str) -> None:
        """广播发送失败事件（HTTP 层已 202 受理，结果经 WS 送达）。"""
        logger.warning("发送失败（商机 #%d）：%s", task.opportunity_id, message)
        self._notifier.broadcast("send_result", {
            "opportunity_id": task.opportunity_id,
            "ok": False,
            "message": message,
        })

    def set_message_lookup(self, lookup) -> None:
        """注入消息查询函数（main.py 组装时调用，避免循环导入）。"""
        self._message_lookup = lookup

# -*- coding: utf-8 -*-
"""微信接入层服务：WeChatDB/Listener 单例封装。

职责（技术方案 T02）：
- 懒加载 wechatauto（导入失败/非 Windows/缺依赖时友好降级，不崩溃）；
- 启停 Listener、注册/注销监控会话回调；
- watermark 定期（30s）与退出时刷回 watermarks 表；
- 会话搜索 / 历史消息查询（供 API 复用）；
- 状态查询（/api/status 数据源）。

wechatauto 只 import 调用，不做任何修改（硬约束）。
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from app import db as dbmod
from app.aggregator import Aggregator
from app.config import (
    CONTEXT_COUNT,
    SESSION_SEARCH_LIMIT_DEFAULT,
    WATERMARK_FLUSH_SEC,
    KEY_RUNTIME_WM_SAVED_AT,
)
from app.db import Database, epoch_to_iso

logger = logging.getLogger(__name__)

# 仓库根目录（wechatauto-replica/）加入 sys.path，保证从 kefu-system 目录
# 启动时也能 import wechatauto（只 import 调用，不做任何修改）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 微信 4.x 主窗口标题（与 wechatauto.guia.WX_MAIN_WIN_TITLE 一致的探测词）
_WX_MAIN_TITLE_CANDIDATES = ("微信", "WeChat")


class WeChatService:
    """微信接入服务（应用生命周期内单例，线程安全）。"""

    def __init__(self, database: Database, aggregator: Aggregator) -> None:
        self._db = database
        self._aggregator = aggregator
        self._wxdb = None            # wechatauto.db.WeChatDB 实例
        self._listener = None        # wechatauto.db.Listener 实例
        self._listener_lock = threading.RLock()
        self._available = False      # wechatauto 是否可用
        self._reason = ""            # 降级原因（中文，/api/status 展示）
        self._flush_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._name_cache: dict[str, str] = {}   # username → display name 缓存
        self._name_cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def start(self) -> None:
        """拉起微信监听（失败降级，不抛异常）。"""
        with self._listener_lock:
            if self._listener is not None:
                return
            try:
                from wechatauto.db import Listener, WeChatDB  # 延迟导入
            except Exception as exc:
                self._degrade(f"微信组件加载失败：{exc}")
                return
            try:
                self._wxdb = WeChatDB()
            except Exception as exc:
                self._degrade(f"微信数据库不可用（微信可能未登录）：{exc}")
                return
            try:
                marks = self._db.load_watermarks()
                self._listener = Listener(self._wxdb, interval=1.0,
                                          watermark=marks or None)
                for username in self._db.list_enabled_target_usernames():
                    self._listener.add_listener(username, self._on_message)
                self._listener.start()
                self._available = True
                self._reason = ""
                logger.info("微信监听已启动（监控会话 %d 个，加载 watermark %d 条）",
                            len(self._db.list_enabled_target_usernames()),
                            len(marks))
            except Exception as exc:
                self._listener = None
                self._degrade(f"微信监听启动失败：{exc}")
                return
            # watermark 定期刷回线程
            self._stop.clear()
            self._flush_thread = threading.Thread(
                target=self._flush_loop, name="kefu-wm-flush", daemon=True)
            self._flush_thread.start()

    def stop(self) -> None:
        """停止监听并把 watermark 刷回数据库。"""
        self._stop.set()
        with self._listener_lock:
            listener = self._listener
            self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                logger.exception("停止 Listener 异常")
        if self._flush_thread:
            self._flush_thread.join(timeout=5)
            self._flush_thread = None
        try:
            marks = listener.watermark if listener is not None else {}
            if marks:
                self._db.save_watermarks(marks)
                self._db.set_setting(KEY_RUNTIME_WM_SAVED_AT,
                                     dbmod.now_iso())
        except Exception:
            logger.exception("退出时刷回 watermark 失败")
        logger.info("微信服务已停止")

    def _degrade(self, reason: str) -> None:
        """记录降级原因（系统继续运行，仅微信相关功能不可用）。"""
        self._available = False
        self._reason = reason
        self._listener = None
        logger.warning("微信接入降级：%s", reason)

    def _flush_loop(self) -> None:
        """每 30 秒把 Listener 内存 watermark 刷回 watermarks 表。"""
        while not self._stop.wait(WATERMARK_FLUSH_SEC):
            try:
                with self._listener_lock:
                    listener = self._listener
                if listener is None:
                    continue
                marks = listener.watermark
                if marks:
                    self._db.save_watermarks(marks)
                    self._db.set_setting(KEY_RUNTIME_WM_SAVED_AT,
                                         dbmod.now_iso())
            except Exception:
                logger.exception("watermark 定期刷回失败（已忽略）")

    # ------------------------------------------------------------------
    # 消息回调（Listener 工作线程调用）
    # ------------------------------------------------------------------
    def _on_message(self, msg: dict, listener=None) -> None:
        """Listener 新消息回调：非文本记 skipped 后丢弃，文本进合并窗口。"""
        try:
            username = str(msg.get("username") or "")
            mtype = str(msg.get("type") or "")
            if mtype != "文本":
                self._db.insert_analysis_log(
                    session_username=username,
                    sender_wxid=str(msg.get("sender_username") or ""),
                    msg_count=1,
                    input_text=str(msg.get("content") or "")[:200],
                    status="skipped",
                    error=f"非文本消息（{mtype or '未知类型'}），不分析",
                )
                return
            kind = "group" if username.endswith("@chatroom") else "friend"
            session_name = self.get_display_name(username)
            # 群内昵称解析（有 wxid 时尽力解析显示名，解析失败回退 wxid）
            sender_wxid = str(msg.get("sender_username") or "")
            display = self.get_display_name(sender_wxid) if sender_wxid else ""
            if display and display != sender_wxid:
                msg["sender_display_name"] = display
            self._aggregator.add(msg, session_name, kind)
        except Exception:
            logger.exception("处理新消息回调异常（已忽略）")

    # ------------------------------------------------------------------
    # 监控对象联动（API 增删改时调用）
    # ------------------------------------------------------------------
    def register_target(self, username: str) -> None:
        """新增监控对象后即时注册监听。"""
        with self._listener_lock:
            if self._listener is None:
                return
            try:
                self._listener.add_listener(username, self._on_message)
                logger.info("已注册监听：%s", username)
            except Exception:
                logger.exception("注册监听失败：%s", username)

    def unregister_target(self, username: str) -> None:
        """删除/停用监控对象后注销监听。"""
        with self._listener_lock:
            if self._listener is None:
                return
            try:
                self._listener.remove_listener(username, self._on_message)
                logger.info("已注销监听：%s", username)
            except Exception:
                logger.exception("注销监听失败：%s", username)

    # ------------------------------------------------------------------
    # 查询能力（API 复用）
    # ------------------------------------------------------------------
    def get_display_name(self, username: str) -> str:
        """username → 显示名（联系人/群名；失败回退 username，带缓存）。"""
        if not username:
            return ""
        with self._name_cache_lock:
            hit = self._name_cache.get(username)
        if hit:
            return hit
        name = username
        if self._wxdb is not None:
            try:
                name = self._wxdb.get_nickname(username) or username
            except Exception:
                pass
        with self._name_cache_lock:
            self._name_cache[username] = name
        return name

    def search_sessions(self, keyword: str = "",
                        limit: int = SESSION_SEARCH_LIMIT_DEFAULT,
                        kind: str = "all") -> list[dict]:
        """会话搜索（群 + 好友）：复用 get_sessions / search_contact / get_contacts。

        Args:
            kind: `all` / `group` / `friend`，按类型过滤返回结果
               （设置页监控对象双栏左栏按类型拉取候选列表用）。

        Returns:
            [{username, display_name, kind}]
        """
        kind = kind if kind in ("group", "friend", "all") else "all"
        limit = max(1, min(limit, 100))
        out: dict[str, dict] = {}
        if self._wxdb is None:
            return []

        def _accept(item_kind: str) -> bool:
            return kind == "all" or item_kind == kind

        try:
            # 会话列表（最近活跃优先）
            for s in self._wxdb.get_sessions(limit=100):
                username = str(s.get("username") or "")
                if not username or username in out:
                    continue
                if username.endswith("@chatroom"):
                    item_kind, name = "group", self.get_display_name(username)
                elif username.startswith("gh_"):
                    continue  # 公众号不参与监控
                else:
                    item_kind, name = "friend", self.get_display_name(username)
                if not _accept(item_kind):
                    continue
                if keyword and keyword not in name and keyword not in username:
                    continue
                out[username] = {"username": username,
                                 "display_name": name, "kind": item_kind}
                if len(out) >= limit:
                    return list(out.values())
            # 联系人搜索（覆盖不在最近会话里的好友/群）
            contacts = []
            if keyword:
                contacts = self._wxdb.search_contact(keyword)
            elif kind == "group":
                # wechatauto.get_contacts 的群聊取值为 "chatroom"（不是 "group"）
                contacts = self._wxdb.get_contacts(kind="chatroom", limit=limit)
            else:
                contacts = self._wxdb.get_contacts(kind="friend", limit=limit)
            for c in contacts:
                username = str(c.get("username") or "")
                if not username or username in out or username.startswith("gh_"):
                    continue
                item_kind = ("group" if username.endswith("@chatroom")
                             else "friend")
                if not _accept(item_kind):
                    continue
                name = c.get("remark") or c.get("nick_name") or username
                out[username] = {"username": username,
                                 "display_name": name, "kind": item_kind}
                if len(out) >= limit:
                    break
        except Exception as exc:
            logger.warning("会话搜索失败：%s", exc)
        return list(out.values())[:limit]

    def get_messages(self, username: str, limit: int = 50,
                     before_seq: int = 0) -> list[dict]:
        """会话历史消息（时间倒序；before_seq>0 时取 sort_seq 小于它的部分）。

        字段对齐 API 约定：local_id/type/type_name/sender_id/sender_name/
        create_time(ISO8601)/content/sort_seq。
        """
        if self._wxdb is None:
            return []
        limit = max(1, min(limit, 200))
        try:
            fetch = limit if not before_seq else min(200, limit + 100)
            msgs = self._wxdb.get_messages(username, limit=fetch)
        except Exception as exc:
            logger.warning("读取会话消息失败：%s", exc)
            return []
        out: list[dict] = []
        for m in msgs:
            if before_seq and int(m.get("sort_seq") or 0) >= before_seq:
                continue
            sender_wxid = str(m.get("sender_username") or "")
            out.append({
                "local_id": int(m.get("local_id") or 0),
                "type": str(m.get("type") or ""),
                "type_name": str(m.get("type") or ""),
                "sender_id": int(m.get("sender_id") or 0),
                "sender_name": (self.get_display_name(sender_wxid)
                                if sender_wxid else "我"),
                "create_time": epoch_to_iso(m.get("create_time")),
                "content": str(m.get("content") or ""),
                "sort_seq": int(m.get("sort_seq") or 0),
            })
            if len(out) >= limit:
                break
        return out

    def get_message_by_local_id(self, username: str,
                                local_id: int) -> Optional[dict]:
        """按 local_id 取一条消息（引用回复定位 target_text 用）。"""
        if self._wxdb is None:
            return None
        try:
            row = self._wxdb.get_message_row(username, local_id)
        except Exception as exc:
            logger.warning("按 local_id 取消息失败：%s", exc)
            return None
        if not row:
            return None
        content = row.get("content")
        if not isinstance(content, str):
            content = ""
        return {
            "local_id": int(row.get("local_id") or 0),
            "content": content,
            "sender_name": self.get_display_name(
                str(row.get("sender_username") or "")) or "未知",
        }

    def session_exists(self, username: str) -> bool:
        """目标校验：会话存在于联系人/群（发送前校验用）。"""
        if self._wxdb is None:
            return True  # 降级时无法校验，放行交由 Sender 报错
        try:
            if self._wxdb.get_nickname(username):
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """微信接入是否可用（未降级）。"""
        return self._available

    @property
    def degraded_reason(self) -> str:
        """降级原因（空 = 正常）。"""
        return self._reason

    @property
    def listener_running(self) -> bool:
        """Listener 是否在运行。"""
        with self._listener_lock:
            return self._listener is not None

    @property
    def monitored_count(self) -> int:
        """启用中的监控会话数。"""
        return len(self._db.list_enabled_target_usernames())

    @property
    def watermark_saved_at(self) -> str:
        """最近 watermark 刷回时间。"""
        return self._db.get_setting(KEY_RUNTIME_WM_SAVED_AT, "")

    def probe_wechat_window(self) -> bool:
        """探测微信主窗口是否存在（ctypes FindWindow，轻量、不创建 GUI 实例）。"""
        try:
            for title in _WX_MAIN_TITLE_CANDIDATES:
                hwnd = ctypes.windll.user32.FindWindowW(None, title)
                if hwnd:
                    return True
        except Exception:
            pass
        return False


def wait_seconds(seconds: float) -> None:
    """测试辅助：休眠指定秒数。"""
    time.sleep(seconds)

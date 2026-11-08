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
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from app import db as dbmod
from app.aggregator import Aggregator
from app.config import (
    CONTEXT_COUNT,
    GROUP_MEMBER_CACHE_TTL_SEC,
    MSG_QUERY_RETRY_INTERVAL_SEC,
    MSG_QUERY_RETRY_TIMES,
    SESSION_CACHE_TTL_SEC,
    SESSION_SEARCH_LIMIT_DEFAULT,
    WATERMARK_FLUSH_SEC,
    KEY_RUNTIME_WM_SAVED_AT,
)
from app.db import Database, epoch_to_iso

logger = logging.getLogger(__name__)

# 微信库读取失败的用户可读提示（前端展示 + 重新加载按钮）
MSG_DB_BUSY_ERROR = "微信数据库正忙（微信客户端正在写入），请稍后点击重新加载"
# 微信服务未接入时的提示
MSG_DB_UNAVAILABLE_ERROR = "微信数据库不可用，请确认微信已登录"

# 微信 4.x 群消息正文自带的发送者前缀（wxid_xxx: / 自定义微信号:），
# 原生格式为 "发送者ID:\n正文"；sender_id 映射不可靠，此前缀为权威发送者。
# 显示渲染侧用（逐行处理，行内无换行，$ 锚点安全）：
_SENDER_PREFIX_RE = re.compile(r"^([^\s:：]{2,64})\s*:\s*(.*)$")
# 发送者提取侧用（严格版）：
# - ID 字符集排除 "/"（URL 路径字符）；
# - 冒号后紧跟 "//" 视为 URL scheme（如 http://...），不提取（BUG-3）；
# - 提取时先按换行切分仅取首行，避免多行正文匹配失败回退到不可靠
#   sender_username 映射（BUG-1，见 extract_content_sender）。
_SENDER_ID_STRICT_RE = re.compile(r"^([^\s:：/]{2,64})\s*:(?!//)\s*")
# 合并文本行首的 "[HH:MM] " 时间前缀（Aggregator.merged_text 系统加的）
_TIME_PREFIX_RE = re.compile(r"^\[\d{1,2}:\d{2}\]\s*")

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
        # 全量联系人/会话列表缓存（TTL 60s；会话搜索改为缓存 + 内存过滤，
        # 避免每次搜索都撞微信库冷分片/解密导致的秒级等待）
        self._contact_cache: list[dict] = []
        self._contact_cache_ts: float = 0.0
        self._contact_cache_lock = threading.Lock()
        # single-flight 并发构建锁：缓存未命中时仅首个线程持锁构建
        # （真实构建可达 ~20s），其余线程等待后复用，避免并发首请求
        # 重复撞微信库冷分片（前端设置页本身就会并行发两个搜索请求）
        self._contact_cache_build_lock = threading.Lock()
        # 群成员显示名缓存（chatroom → (timestamp, {username: 显示名})，TTL 600s）
        self._group_members_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._group_members_lock = threading.Lock()

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
            kind = "group" if username.endswith("@chatroom") else "friend"
            sender_wxid, sender_name = self._resolve_sender(username, kind, msg)
            if mtype != "文本":
                self._db.insert_analysis_log(
                    session_username=username,
                    sender_wxid=sender_wxid,
                    msg_count=1,
                    input_text=str(msg.get("content") or "")[:200],
                    status="skipped",
                    error=f"非文本消息（{mtype or '未知类型'}），不分析",
                )
                return
            session_name = self.get_display_name(username)
            # 发送人已用可靠数据源解析（群=正文前缀 / 私聊=会话对方），
            # 覆盖 msg 中不可靠的 sender_username 供 Aggregator/管线透传
            msg["sender_username"] = sender_wxid
            msg["sender_display_name"] = sender_name
            self._aggregator.add(msg, session_name, kind)
        except Exception:
            logger.exception("处理新消息回调异常（已忽略）")

    # ------------------------------------------------------------------
    # 发送人权威解析（修复：SenderName2Id rowid 映射不可靠导致的显示错误）
    # ------------------------------------------------------------------
    @staticmethod
    def extract_content_sender(content: str) -> str:
        """从群消息正文提取行首发送者 ID（wxid_xxx: / 自定义微信号:）。

        微信 4.x 原生把发送者写入正文首部（``ID:\\n正文``），该前缀是
        权威数据源；无前缀返回空串（由调用方回退 sender_username）。

        实现细节：
        - 先按换行切分仅取首行再匹配（不用 ``$`` 锚点直接匹配全文，
          否则 ``ID:\\n多行正文`` 会匹配失败——BUG-1）；
        - ID 字符集排除 "/"，且冒号后紧跟 "//"（URL scheme，如
          ``http://...`` 开头的无前缀正文）不提取——BUG-3。
        """
        first_line = str(content or "").lstrip().split("\n", 1)[0]
        match = _SENDER_ID_STRICT_RE.match(first_line)
        return match.group(1) if match else ""

    def _get_group_member_names(self, chatroom: str) -> dict[str, str]:
        """群成员 username → 显示名映射（群内昵称优先，缓存 TTL 600s）。

        wechatauto.get_group_members 返回字段：username / nick_name /
        remark / is_owner；群内显示名取 nick_name（群昵称），缺失回退
        remark。读取失败回退空映射（调用方再回退联系人/原值）。
        """
        with self._group_members_lock:
            cached = self._group_members_cache.get(chatroom)
            if cached and \
                    time.monotonic() - cached[0] < GROUP_MEMBER_CACHE_TTL_SEC:
                return cached[1]
        names: dict[str, str] = {}
        if self._wxdb is not None:
            try:
                for member in self._wxdb.get_group_members(chatroom):
                    uid = str(member.get("username") or "")
                    if uid:
                        names[uid] = str(member.get("nick_name")
                                         or member.get("remark") or uid)
            except Exception as exc:
                logger.warning("读取群成员列表失败（%s）：%s", chatroom, exc)
        with self._group_members_lock:
            self._group_members_cache[chatroom] = (time.monotonic(), names)
        return names

    def resolve_group_member_display(self, chatroom: str,
                                     member_id: str) -> str:
        """群成员 ID → 群内显示名。

        优先级：chatroom member 表群昵称 → 联系人备注/昵称（get_nickname）
        → 原 ID（解析失败回退原值，不崩管线）。
        """
        if not member_id:
            return ""
        if chatroom.endswith("@chatroom"):
            names = self._get_group_member_names(chatroom)
            if member_id in names:
                return names[member_id]
        if self._wxdb is not None:
            try:
                nick = self._wxdb.get_nickname(member_id) or ""
            except Exception:
                nick = ""
            if nick:
                return nick
        return member_id

    def _resolve_sender(self, username: str, kind: str,
                        msg: dict) -> tuple[str, str]:
        """解析消息发送人的权威 (wxid, 显示名)。

        背景：msg["sender_username"] 来自 message_resource.db 的
        SenderName2Id 表 rowid→user_name 映射，该映射不可靠（私聊可能
        解析成无关好友、群内非好友可能解析成好友昵称）。改用可靠数据源：

        - 群聊：正文自带发送者前缀（``ID:\\n正文``）为权威，仅当无前缀时
          回退 sender_username；显示名 = 群成员表群昵称 → 联系人备注/昵称
          → 原 ID；
        - 私聊：发送人就是会话对方，与 sender_username 映射无关；
          sender_id ∈ (2, "2") 为自己发送（显示「我」，后续管线正常处理）。
        """
        if kind != "group":
            if msg.get("sender_id") in (2, "2"):
                return "", "我"
            return username, self.get_display_name(username)
        # 群聊：正文前缀优先，sender_username 映射仅作回退
        sender_id = self.extract_content_sender(msg.get("content") or "") \
            or str(msg.get("sender_username") or "")
        if not sender_id:
            return "", ""
        return sender_id, self.resolve_group_member_display(username, sender_id)

    # ------------------------------------------------------------------
    # 分析日志展示字段（来源会话/消息内容的显示名渲染）
    # ------------------------------------------------------------------
    def build_log_displays(self, session_username: str,
                           input_text: str) -> tuple[str, str]:
        """分析日志的展示字段：(session_display, input_text_display)。

        - session_display：会话显示名（备注/昵称优先，失败回退原 ID）；
        - input_text_display：群消息每行的发送者前缀解析为显示名
          （私聊无前缀行，原样返回）；前缀解析失败保留原值。
        """
        session_username = str(session_username or "")
        input_text = str(input_text or "")
        session_display = self.get_display_name(session_username) \
            or session_username
        if not session_username.endswith("@chatroom"):
            return session_display, input_text
        return session_display, self._render_input_text_display(
            session_username, input_text)

    def _render_input_text_display(self, chatroom: str,
                                   text: str) -> str:
        """把合并文本中每行的发送者 ID 前缀替换为显示名。

        行格式兼容 ``[HH:MM] wxid_xxx:`` / ``[HH:MM] wxid_xxx: 正文`` /
        无时间前缀的 ``wxid_xxx:`` 行；前缀若解析不出显示名（既不在群
        成员表也不是已知联系人）则整行保留原值，避免误伤正文。
        """
        out_lines: list[str] = []
        for line in text.split("\n"):
            time_match = _TIME_PREFIX_RE.match(line)
            time_prefix = line[:time_match.end()] if time_match else ""
            body = line[time_match.end():] if time_match else line
            match = _SENDER_PREFIX_RE.match(body)
            if not match:
                out_lines.append(line)
                continue
            prefix_id, rest = match.group(1), match.group(2)
            display = self._try_resolve_chat_id(chatroom, prefix_id)
            if display and display != prefix_id:
                out_lines.append(f"{time_prefix}{display}: {rest}".rstrip())
            else:
                out_lines.append(line)
        return "\n".join(out_lines)

    def _try_resolve_chat_id(self, chatroom: str, uid: str) -> str:
        """会话内 ID → 显示名；解析失败返回空串（调用方保留原值）。"""
        if not uid:
            return ""
        if chatroom.endswith("@chatroom"):
            names = self._get_group_member_names(chatroom)
            if uid in names:
                return names[uid]
        if self._wxdb is not None:
            try:
                nick = self._wxdb.get_nickname(uid) or ""
            except Exception:
                nick = ""
            if nick:
                return nick
        return ""

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

    def _build_contact_list(self) -> list[dict]:
        """构建全量联系人/会话列表（群+好友，含显示名）。

        会话（≤100 条，最近活跃优先）逐个解析显示名；联系人用
        wechatauto 自带的 remark/nick_name 快照，免逐个解析（快）。
        公众号（gh_ 前缀）不参与监控，直接跳过。
        """
        items: list[dict] = []
        seen: set[str] = set()
        for s in self._wxdb.get_sessions(limit=100):
            username = str(s.get("username") or "")
            if not username or username in seen or username.startswith("gh_"):
                continue
            kind = "group" if username.endswith("@chatroom") else "friend"
            items.append({"username": username,
                          "display_name": self.get_display_name(username),
                          "kind": kind})
            seen.add(username)
        # wechatauto.get_contacts 的群聊取值为 "chatroom"（不是 "group"）
        for c_kind, item_kind in (("chatroom", "group"), ("friend", "friend")):
            try:
                contacts = self._wxdb.get_contacts(kind=c_kind, limit=2000)
            except Exception as exc:
                logger.warning("读取联系人（%s）失败：%s", c_kind, exc)
                contacts = []
            for c in contacts:
                username = str(c.get("username") or "")
                if not username or username in seen or username.startswith("gh_"):
                    continue
                name = c.get("remark") or c.get("nick_name") or username
                items.append({"username": username,
                              "display_name": name, "kind": item_kind})
                seen.add(username)
        return items

    def _get_cached_contact_list(self) -> list[dict]:
        """全量联系人/会话列表（TTL 60s 内存缓存 + single-flight 构建）。

        流程：锁内快查 → 未命中则竞争构建锁 → 持锁线程构建并写缓存，
        其余线程等待后二次检查直接复用（不重复构建）；构建失败回退
        过期缓存且不更新时间戳（下次请求会再尝试重建）。
        """
        with self._contact_cache_lock:
            if self._contact_cache and \
                    time.monotonic() - self._contact_cache_ts < SESSION_CACHE_TTL_SEC:
                return self._contact_cache
        with self._contact_cache_build_lock:
            # 拿到构建锁后二次检查：等待期间可能已有线程完成构建
            with self._contact_cache_lock:
                if self._contact_cache and \
                        time.monotonic() - self._contact_cache_ts \
                        < SESSION_CACHE_TTL_SEC:
                    return self._contact_cache
            try:
                fresh = self._build_contact_list()
            except Exception as exc:
                # 构建失败：回退过期缓存（聊胜于无），不更新时间戳，
                # 下次请求再重试构建
                logger.warning("构建联系人列表缓存失败：%s", exc)
                with self._contact_cache_lock:
                    return list(self._contact_cache)
            with self._contact_cache_lock:
                self._contact_cache = fresh
                self._contact_cache_ts = time.monotonic()
                return self._contact_cache

    # 说明：未提供 invalidate_contact_cache 之类的手动失效方法——
    # 监控对象的增删只改本系统 monitor_targets 表，不影响微信联系人库
    # 的内容，联系人列表缓存（TTL 60s）无需随之失效；微信侧新出现的
    # 会话最长 60 秒后即可被搜到，属可接受的缓存延迟。

    def search_sessions(self, keyword: str = "",
                        limit: int = SESSION_SEARCH_LIMIT_DEFAULT,
                        kind: str = "all") -> list[dict]:
        """会话搜索（群 + 好友）：TTL 缓存的全量列表 + 内存过滤。

        首次调用构建缓存（可能较慢，微信库冷启动解密分片）；
        之后 60 秒内的搜索/翻页/类型切换全部内存过滤（毫秒级）。

        Args:
            keyword: 显示名/username 子串匹配，空串返回全部。
            kind: `all` / `group` / `friend`，按类型过滤返回结果。

        Returns:
            [{username, display_name, kind}]
        """
        kind = kind if kind in ("group", "friend", "all") else "all"
        limit = max(1, min(limit, 100))
        if self._wxdb is None:
            return []
        out: list[dict] = []
        for item in self._get_cached_contact_list():
            if kind != "all" and item["kind"] != kind:
                continue
            if keyword and keyword not in item["display_name"] \
                    and keyword not in item["username"]:
                continue
            out.append(dict(item))
            if len(out) >= limit:
                break
        return out

    def get_messages(self, username: str, limit: int = 50,
                     before_seq: int = 0) -> list[dict]:
        """会话历史消息（时间倒序；before_seq>0 时取 sort_seq 小于它的部分）。

        字段对齐 API 约定：local_id/type/type_name/sender_id/sender_name/
        create_time(ISO8601)/content/sort_seq。
        持久失败时返回空列表（与「会话确实无消息」同形，调用方按需区分）。
        """
        msgs, _error = self.get_messages_with_error(username, limit=limit,
                                                    before_seq=before_seq)
        return msgs

    def get_messages_with_error(self, username: str, limit: int = 50,
                                before_seq: int = 0,
                                retry_on_empty: bool = False
                                ) -> tuple[list[dict], str]:
        """会话历史消息查询（含瞬时故障重试），返回 (msgs, error)。

        优化：微信客户端写库期间分片瞬时 malformed → 查询抛异常或返回空，
        此处间隔 MSG_QUERY_RETRY_INTERVAL_SEC 重试最多 MSG_QUERY_RETRY_TIMES 次
        （上下文场景足够等到重建完成）。

        Args:
            retry_on_empty: 空结果也参与重试（上下文场景：商机存在 ⇒ 会话应有消息）。

        Returns:
            (msgs, error)：error 为空串 = 成功；
            非空 = 用户可读中文原因（异常重试耗尽 → 微信数据库正忙）；
            「重试后仍为空」视为会话确实无消息，error 返回空串。
        """
        if self._wxdb is None:
            return [], MSG_DB_UNAVAILABLE_ERROR
        limit = max(1, min(limit, 200))
        attempt = 0
        while True:
            msgs, failed = self._fetch_messages_once(username, limit, before_seq)
            if not failed and (msgs or not retry_on_empty):
                return msgs, ""
            # 查询异常，或（空结果且要求重试）：间隔后重试
            if attempt >= MSG_QUERY_RETRY_TIMES:
                if failed:
                    return [], MSG_DB_BUSY_ERROR
                return [], ""  # 空结果重试耗尽：按「会话确实无消息」处理
            attempt += 1
            logger.warning("读取会话消息%s（第 %d 次尝试，%s），%.1fs 后重试",
                           "异常" if failed else "为空", attempt, username,
                           MSG_QUERY_RETRY_INTERVAL_SEC)
            time.sleep(MSG_QUERY_RETRY_INTERVAL_SEC)

    def _fetch_messages_once(self, username: str, limit: int,
                             before_seq: int) -> tuple[list[dict], bool]:
        """单次消息查询；返回 (msgs, failed)，异常不外抛。"""
        try:
            fetch = limit if not before_seq else min(200, limit + 100)
            msgs = self._wxdb.get_messages(username, limit=fetch)
        except Exception as exc:
            logger.warning("读取会话消息失败（%s）：%s", username, exc)
            return [], True
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
        return out, False

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

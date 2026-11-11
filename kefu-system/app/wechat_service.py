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
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from app import db as dbmod
from app import content_format
from app import voice_asr
from app.aggregator import Aggregator
from app.config import (
    CONTEXT_COUNT,
    GROUP_MEMBER_CACHE_TTL_SEC,
    IMG_CACHE_DIR,
    LEGACY_CACHE_ROOT,
    MSG_QUERY_RETRY_INTERVAL_SEC,
    MSG_QUERY_RETRY_TIMES,
    SESSION_CACHE_TTL_SEC,
    SESSION_SEARCH_LIMIT_DEFAULT,
    VOICE_CACHE_DIR,
    WATERMARK_FLUSH_SEC,
    KEY_ASR_ENABLED,
    KEY_ASR_MAX_SECONDS,
    KEY_RUNTIME_CACHE_DIR,
    KEY_RUNTIME_WM_SAVED_AT,
    CACHE_DIR_DEFAULT,
    dir_usage,
    resolve_cache_dir,
)
from app.db import Database, epoch_to_iso
from app.voice_retry import VoiceRetryQueue

logger = logging.getLogger(__name__)

# 微信库读取失败的用户可读提示（前端展示 + 重新加载按钮）
MSG_DB_BUSY_ERROR = "微信数据库正忙（微信客户端正在写入），请稍后点击重新加载"
# 微信服务未接入时的提示
MSG_DB_UNAVAILABLE_ERROR = "微信数据库不可用，请确认微信已登录"
# 语音转写相关提示（用户可读，直接进分析日志 error 字段）
MSG_ASR_DISABLED = "语音转写已关闭（可在设置 → 数据与账单 开启）"
# 「音频还没落库」是可恢复状态（微信按需下载语音），文案要能让用户自己
# 解决，而不是读起来像系统坏了。
MSG_ASR_NO_VOICE = "微信尚未下载该语音（在微信里点开播放一次即可）"

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

# 微信内置系统会话 / 占位账号：不对应真人或真实群，参与监控毫无意义，
# 但它们在 contact/session 表里与普通联系人同形，且按 username 排序时
# 往往排在很前面，会把「添加监控」的默认候选列表刷成垃圾。
_SYSTEM_ACCOUNTS = frozenset({
    "brandsessionholder", "brandservicesessionholder", "filehelper",
    "weixin", "wechatteam", "mphelper", "qqmail", "qmessage", "qqsafe",
    "qqsync", "floatbottle", "medianote", "fmessage", "tmessage",
    "newsapp", "notifymessage", "weibo", "facebook", "twitter",
    "linkedin", "weixinreminder", "reminder", "exmail_tool",
})


def _is_system_account(username: str) -> bool:
    """是否微信内置系统会话（文件传输助手 / 品牌会话占位 / 各类助手号）。"""
    u = (username or "").strip().lower()
    if not u:
        return True
    return u in _SYSTEM_ACCOUNTS or u.startswith("brandsessionholder") \
        or u.startswith("brandservicesession")

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
        self._group_members_cache: dict[str, tuple[float, dict[str, str], bool]] = {}
        self._group_members_lock = threading.Lock()
        # 媒体下载器懒加载单例（MediaDownloader 首次使用会解密媒体库，约 13s，
        # 不能每条消息/每张图片都新建一个）。图片与语音共用同一实例。
        self._media_downloader = None
        self._media_lock = threading.Lock()
        # 语音转写的时长上限由 settings 热更（asr.max_seconds），交给转写器
        # 每次识别时实时读取，避免改设置后需要重启服务。
        if database is not None:
            voice_asr.get_transcriber().set_max_seconds_provider(
                lambda: database.get_int(KEY_ASR_MAX_SECONDS,
                                         voice_asr.MAX_SECONDS_DEFAULT))
        # 语音补转写队列：微信语音是「消息先到、音频后到」（音频在客户端
        # 按需下载后才写进 media_*.db），监听器秒级轮询导致首次转写几乎
        # 必然撞上「音频未落库」。失败不丢消息，按退避序列自动重试，补上
        # 后再回填日志并重新纳入分析（见 app/voice_retry.py）。
        self._voice_retry = VoiceRetryQueue(
            transcribe=self.transcribe_voice,
            on_success=self._on_voice_retry_success,
            on_failure=self._on_voice_retry_failure,
        )

    # ------------------------------------------------------------------
    # 缓存目录（可配置；默认 kefu-system/data/wechat_cache）
    # ------------------------------------------------------------------
    def configured_cache_dir(self) -> Path:
        """当前配置的缓存目录（绝对路径；配置为空时用默认目录）。"""
        raw = self._db.get_setting(KEY_RUNTIME_CACHE_DIR, "")
        return resolve_cache_dir(raw)

    def cache_dir_info(self) -> dict:
        """缓存目录信息（设置页展示 / 保存后回显）。

        ``active`` 表示当前监听实例是否已经在用这个目录：改配置后必须重启
        服务才会切换，这里如实反映，避免用户以为立即生效。
        """
        configured = self._db.get_setting(KEY_RUNTIME_CACHE_DIR, "")
        path = resolve_cache_dir(configured)
        size, count = dir_usage(path)
        wxdb = self._wxdb
        active_dir = str(getattr(wxdb, "workdir", "") or "") if wxdb else ""
        return {
            "dir": str(path),
            "default_dir": str(CACHE_DIR_DEFAULT),
            "configured": configured,
            "is_default": not str(configured).strip(),
            "size_bytes": size,
            "file_count": count,
            "active_dir": active_dir,
            "active": bool(active_dir) and active_dir == str(path),
            "legacy_dirs": [str(d) for d in self._legacy_cache_dirs()],
        }

    @staticmethod
    def _legacy_cache_dirs() -> list[Path]:
        """旧版本默认缓存位置（%TEMP%/wechatauto_db/<account>）。"""
        try:
            return [d for d in LEGACY_CACHE_ROOT.iterdir() if d.is_dir()]
        except OSError:
            return []

    @staticmethod
    def _dir_size_mb(path: Path) -> float:
        size, _ = dir_usage(path)
        return size / 1048576.0

    def _adopt_legacy_keys(self, target: Path) -> None:
        """把旧 TEMP 目录里的密钥缓存搬到新目录（仅当目标缺失）。

        不搬的话首次启动要重新从微信进程内存提取密钥，微信升级后可能提取
        失败（表现为"数据库不可用"）。keys.json 很小，且 WeChatDB 会对每个
        库做页 1 HMAC 校验、不匹配就重新提取，所以即使账号不同也不会用错。
        """
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("缓存目录创建失败（%s）：%s", target, exc)
            return
        for name in ("keys.json", "image_keys.json"):
            dst = target / name
            if dst.exists():
                continue
            best: Optional[Path] = None
            best_mtime = 0.0
            for d in self._legacy_cache_dirs():
                src = d / name
                try:
                    if src.is_file() and src.stat().st_mtime >= best_mtime:
                        best, best_mtime = src, src.stat().st_mtime
                except OSError:
                    continue
            if best is None:
                continue
            try:
                shutil.copy2(best, dst)
                logger.info("已迁移密钥缓存：%s → %s", best, dst)
            except OSError as exc:
                logger.warning("密钥缓存迁移失败（%s）：%s", best, exc)

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def start(self) -> None:
        """拉起微信监听（失败降级，不抛异常）。"""
        # 补转写队列独立于微信监听可用性：即使微信库暂时不可用也先跑起来，
        # 任务失败会自然耗尽重试（不占用监听线程）。
        self._voice_retry.start()
        with self._listener_lock:
            if self._listener is not None:
                return
            try:
                from wechatauto.db import Listener, WeChatDB  # 延迟导入
            except Exception as exc:
                self._degrade(f"微信组件加载失败：{exc}")
                return
            try:
                # 缓存目录可配置（默认 kefu-system/data/wechat_cache）：
                # 不再落在 %TEMP% 下，避免被系统清理后全量重新解密。
                cache_dir = self.configured_cache_dir()
                self._adopt_legacy_keys(cache_dir)
                self._wxdb = WeChatDB(workdir=str(cache_dir))
                # 换了 wxdb 实例：旧的媒体下载器绑在旧实例上，必须丢弃
                self._media_downloader = None
                logger.info("微信数据库缓存目录：%s（%.1f MB）",
                            cache_dir, self._dir_size_mb(cache_dir))
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
        try:
            self._voice_retry.stop()
        except Exception:
            logger.exception("停止语音补转写队列异常（已忽略）")
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
        """Listener 新消息回调：非文本记 skipped 后丢弃，文本进合并窗口。

        语音消息特殊处理：先本地离线转写为文字，成功则改写 content 为
        ``[语音] 文本`` 并按**文本消息**正常进合并窗口参与商机分析；
        失败（开关关闭 / 模型未装 / 解码失败）保持原 skipped 行为，
        error 里写清原因，便于用户在分析日志里定位问题。
        """
        try:
            username = str(msg.get("username") or "")
            mtype = str(msg.get("type") or "")
            kind = "group" if username.endswith("@chatroom") else "friend"
            sender_wxid, sender_name = self._resolve_sender(username, kind, msg)
            if mtype != "文本":
                if content_format.is_voice_type(mtype):
                    local_id = int(msg.get("local_id") or 0)
                    voice_text, voice_err = self.transcribe_voice(username,
                                                                 local_id)
                    if not voice_text:
                        self._handle_voice_failure(
                            msg, username=username, kind=kind,
                            sender_wxid=sender_wxid, sender_name=sender_name,
                            mtype=mtype, local_id=local_id,
                            error=voice_err or "未知原因")
                        return
                    # 转写成功：按文本消息继续走聚合窗口（音频不出本机）
                    msg["content"] = content_format.voice_text_content(
                        voice_text)
                else:
                    self._db.insert_analysis_log(
                        session_username=username,
                        sender_wxid=sender_wxid,
                        msg_count=1,
                        # 日志展示友好化：非文本消息存可读摘要而非原始 XML（反馈 3）
                        input_text=content_format.format_content(
                            mtype, str(msg.get("content") or ""))[:200],
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
    # 语音转写失败 → 延迟补转写
    # ------------------------------------------------------------------
    def _is_voice_retryable(self, error: str) -> bool:
        """该失败原因是否值得排队重试。

        只有「音频还没落库」是可恢复的（微信稍后/用户播放后才会下载音频）。
        开关关闭、模型未安装、微信库不可用、标识非法等属于配置/环境问题，
        重试多少次都一样，直接落终态让用户按提示处理。
        """
        return str(error or "") == MSG_ASR_NO_VOICE

    def _handle_voice_failure(self, msg: dict, *, username: str, kind: str,
                              sender_wxid: str, sender_name: str,
                              mtype: str, local_id: int,
                              error: str) -> None:
        """语音转写失败的统一出口：先落占位日志，再按需排入补转写队列。

        占位日志让用户立刻能在分析日志里看到「这条语音没转成、为什么」；
        队列补上后会就地更新这条日志（不新插一条，避免重复）。
        """
        retryable = self._is_voice_retryable(error)
        shown = content_format.format_content(
            mtype, str(msg.get("content") or ""))[:200]
        note = ("；音频稍后落库即可补上，已加入自动重试"
                if retryable else "")
        try:
            log_id = self._db.insert_analysis_log(
                session_username=username,
                sender_wxid=sender_wxid,
                msg_count=1,
                input_text=shown,
                status="skipped",
                error=f"语音转写失败：{error}{note}",
                voice_local_id=local_id or None,
            )
        except Exception:
            logger.exception("写入语音失败日志异常（已忽略）")
            log_id = 0
        if not retryable:
            return
        if local_id <= 0:
            logger.info("语音消息缺少 local_id，无法排队重试（%s）", username)
            return
        self._voice_retry.enqueue(username, local_id, {
            # 消息快照：补转写成功后按文本消息重新走一遍合并窗口
            "msg": {k: v for k, v in msg.items() if k != "content"},
            "kind": kind,
            "sender_wxid": sender_wxid,
            "sender_name": sender_name,
            "log_id": log_id,
        })

    def _on_voice_retry_success(self, username: str, local_id: int, text: str,
                                payload: dict, attempt: int) -> None:
        """补转写成功：回填占位日志，并把这条语音重新纳入一次分析。

        重新分析是必要的：首次失败时这条语音被直接丢弃，没有进入过 AI
        分析；补上文本后按普通文本消息重新入窗口，商机才不会漏。
        """
        shown = content_format.voice_text_content(text)
        log_id = int(payload.get("log_id") or 0)
        if log_id:
            self._db.update_analysis_log_voice(
                log_id, input_text=shown, status="ok",
                error=f"语音延迟到达，第 {attempt} 次重试补转写成功（已重新分析）")
        msg = dict(payload.get("msg") or {})
        if not msg:
            return
        msg["content"] = shown
        msg["sender_username"] = payload.get("sender_wxid") or ""
        msg["sender_display_name"] = payload.get("sender_name") or ""
        # 语音消息的原始 local_id 已在 msg 中（快照保留），聚合器按需对齐
        self._aggregator.add(msg, self.get_display_name(username),
                             payload.get("kind") or "friend")

    def _on_voice_retry_failure(self, username: str, local_id: int, error: str,
                                payload: dict, attempts: int) -> None:
        """重试用尽仍未取到音频：落终态，并给出手动恢复路径。"""
        log_id = int(payload.get("log_id") or 0)
        if not log_id:
            return
        try:
            self._db.update_analysis_log_voice(
                log_id, input_text="[语音]", status="skipped",
                error=(f"语音转写失败：{error}；已自动重试 {attempts} 次仍未取到。"
                       "在微信里点开播放这条语音，再点右侧「重新识别」即可补上"))
        except Exception:
            logger.exception("回填语音失败日志异常（已忽略）")

    def retry_voice(self, username: str, local_id: int,
                    log_id: int = 0) -> tuple[str, str]:
        """手动补转写（分析日志「重新识别」按钮）。

        与自动重试同一条链路：转写成功 → 回填日志 → 重新纳入分析。区别是
        消息快照已不在手里，用微信库里的消息行重建（拿不到时间就跳过重新
        分析，只回填文本，仍能满足「看看这条语音说了什么」）。
        """
        username = str(username or "")
        try:
            local_id = int(local_id)
        except (TypeError, ValueError):
            return "", "语音消息标识非法"
        text, error = self.transcribe_voice(username, local_id)
        if not text:
            return "", error or "语音转写失败"
        shown = content_format.voice_text_content(text)
        if log_id:
            try:
                self._db.update_analysis_log_voice(
                    log_id, input_text=shown, status="ok",
                    error="手动重新识别成功（已重新分析）")
            except Exception:
                logger.exception("回填语音日志异常（已忽略）")
        msg = self._rebuild_voice_msg(username, local_id, shown)
        if msg is not None:
            try:
                kind = ("group" if username.endswith("@chatroom")
                        else "friend")
                sender_wxid, sender_name = self._resolve_sender(
                    username, kind, msg)
                msg["sender_username"] = sender_wxid
                msg["sender_display_name"] = sender_name
                self._aggregator.add(msg, self.get_display_name(username),
                                     kind)
            except Exception:
                logger.exception("手动重试后重新纳入分析失败（已忽略）")
        return text, ""

    def _rebuild_voice_msg(self, username: str, local_id: int,
                           shown: str) -> Optional[dict]:
        """用手动重试场景重建一条可入窗口的消息；拿不到返回 None。"""
        wxdb = self._wxdb
        if wxdb is None:
            return None
        try:
            row = wxdb.get_message_row(username, local_id)
        except Exception:
            logger.exception("读取语音消息行失败（跳过重新分析）")
            return None
        if not row:
            return None
        return {
            "username": username,
            "type": "语音",
            "content": shown,
            "local_id": local_id,
            "create_time": row.get("create_time") or 0,
            "sender_id": row.get("sender_id"),
            "sender_username": row.get("sender_username") or "",
        }

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
        """群成员 username → 显示名映射（好友备注优先，缓存 TTL 600s）。

        wechatauto.get_group_members 返回字段：username / nick_name /
        remark / is_owner。显示名优先级（与产品口径一致）：

        1. ``remark``：我对该成员的好友备注（好友场景，如群内昵称被对方
           自定义为花名时仍显示备注）；
        2. ``nick_name``：contact 表昵称——对群内非好友，微信会把其
           群内显示名同步为 contact 昵称（实测 nickname 即群名片）；
        3. 回退成员 username。

        读取失败回退空映射，但失败结果只缓存 15 秒（避免微信写库期
        读取抖动把空映射钉死 600 秒，导致整段时间内发送人显示错误）。
        """
        with self._group_members_lock:
            cached = self._group_members_cache.get(chatroom)
            if cached:
                ts, names, ok = cached
                ttl = GROUP_MEMBER_CACHE_TTL_SEC if ok else 15.0
                if time.monotonic() - ts < ttl:
                    return names
        names: dict[str, str] = {}
        ok = False
        if self._wxdb is not None:
            try:
                for member in self._wxdb.get_group_members(chatroom):
                    uid = str(member.get("username") or "")
                    if uid:
                        names[uid] = (str(member.get("remark") or "")
                                      or str(member.get("nick_name") or "")
                                      or uid)
                ok = True
            except Exception as exc:
                logger.warning("读取群成员列表失败（%s）：%s", chatroom, exc)
        with self._group_members_lock:
            self._group_members_cache[chatroom] = (time.monotonic(), names, ok)
        return names

    def _group_member_reverse_names(self, chatroom: str) -> dict[str, str]:
        """群成员 显示名 → username 反查映射（同名取第一个，仅供参考）。"""
        names = self._get_group_member_names(chatroom)
        reverse: dict[str, str] = {}
        for uid, display in names.items():
            reverse.setdefault(display, uid)
        return reverse

    def resolve_group_member_display(self, chatroom: str,
                                     member_id: str) -> str:
        """群成员 ID → 群内显示名。

        优先级：好友备注 → 群显示名（群成员映射，见
        _get_group_member_names）→ 联系人备注/昵称（get_nickname）→
        原 ID（解析失败回退原值，不崩管线）。
        """
        if not member_id:
            return ""
        if chatroom.endswith("@chatroom"):
            names = self._get_group_member_names(chatroom)
            if member_id in names:
                return names[member_id]
            # 前缀解析出的可能是群显示名而非 wxid（微信 4.x 群消息正文
            # 前缀两种格式都存在）：反查回规范 ID 对应的显示名。
            uid = self._group_member_reverse_names(chatroom).get(member_id)
            if uid:
                return names[uid]
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

        - 群聊：正文自带发送者前缀（``ID:\\n正文`` 或 ``群显示名:\\n正文``）
          为权威——先按 wxid 提取，提取不到（或提到的是显示名）时反查
          群成员映射；仅当两者都失败时才回退 sender_username；
          显示名 = 好友备注 → 群显示名 → 联系人备注/昵称 → 原 ID；
        - 私聊：发送人就是会话对方，与 sender_username 映射无关；
          sender_id ∈ (2, "2") 为自己发送（显示「我」，后续管线正常处理）。
        """
        if kind != "group":
            if msg.get("sender_id") in (2, "2"):
                return "", "我"
            return username, self.get_display_name(username)
        # 群聊：正文前缀优先，sender_username 映射仅作最终回退
        content = str(msg.get("content") or "")
        sender_id = self.extract_content_sender(content)
        if sender_id and username.endswith("@chatroom"):
            names = self._get_group_member_names(username)
            if sender_id not in names:
                # 提取到的可能是群显示名（非 wxid 前缀）：反查真实成员
                uid = self._group_member_reverse_names(username).get(sender_id)
                if uid:
                    sender_id = uid
        if not sender_id:
            # 无前缀或前缀含空格等导致严格正则未命中：尝试「显示名:」
            # 宽松前缀反查（如「长信 无欲无求:」→ 真实 wxid），避免掉进
            # 不可靠的 SenderName2Id 映射
            sender_id = self._sender_from_display_prefix(username, content)
        if not sender_id:
            sender_id = str(msg.get("sender_username") or "")
        if not sender_id:
            return "", ""
        return sender_id, self.resolve_group_member_display(username, sender_id)

    def _sender_from_display_prefix(self, chatroom: str,
                                    content: str) -> str:
        """正文首行「显示名:」前缀 → 群成员 username（失败返回空串）。

        兼容 ``[HH:MM] 显示名:`` / ``显示名: 正文`` 格式；显示名需与群
        成员映射中的显示名精确相等才采纳，避免误伤正文。
        """
        if not chatroom.endswith("@chatroom"):
            return ""
        first_line = content.lstrip().split("\n", 1)[0]
        # 去掉可选的时间前缀（如「[14:02] 」）
        first_line = re.sub(r"^\[[^\]]*\]\s*", "", first_line)
        match = re.match(r"^([^:：]{1,64}?)\s*[:：]", first_line)
        if not match:
            return ""
        display = match.group(1).strip()
        if not display:
            return ""
        return self._group_member_reverse_names(chatroom).get(display, "")

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

    @staticmethod
    def is_openim(username: str) -> bool:
        """是否企业微信/客服类会话（@openim、@kefu.openim 等）。

        微信 4.x 的 contact 表里这类账号 local_type 为 5/6/7，username
        统一以 ``@openim``（企业微信联系人）或 ``@kefu``（客服）结尾。
        它们本质是一对一私聊，可监控、可回复，但历史上被 friend 过滤器
        以「不是个人好友」为由排除，导致全部搜不到。
        """
        u = (username or "").strip().lower()
        return u.endswith("@openim") or "@kefu" in u

    def _build_contact_list(self) -> list[dict]:
        """构建全量联系人/会话列表（群 + 好友 + 企业微信联系人，含显示名）。

        会话（≤100 条，最近活跃优先）逐个解析显示名；联系人用
        wechatauto 自带的 remark/nick_name 快照，免逐个解析（快）。
        公众号（gh_ 前缀）不参与监控，直接跳过。

        好友与群聊均取**全量**（``limit=0``）：本机好友可达 3 万条，
        原先固定 ``limit=2000`` 会把按 username 排序后的绝大多数好友
        截断掉，表现为「明明有这个人却搜不到」。

        企业微信联系人单独取 ``kind="openim"`` 并归入 friend（一对一
        私聊），带 ``openim=True`` 标记供前端区分同名联系人；能解析出
        所属企业时显示名按微信端习惯拼成「昵称@企业简称」。
        """
        corp_map = self._openim_corp_map()
        items: list[dict] = []
        seen: set[str] = set()
        for s in self._wxdb.get_sessions(limit=100):
            username = str(s.get("username") or "")
            if not username or username in seen or username.startswith("gh_") \
                    or _is_system_account(username):
                continue
            openim = self.is_openim(username)
            corp = corp_map.get(username, "")
            items.append({"username": username,
                          "display_name": self._with_corp(
                              self.get_display_name(username), corp),
                          "kind": "group" if username.endswith("@chatroom")
                                  else "friend",
                          "openim": openim, "corp": corp})
            seen.add(username)
        # wechatauto.get_contacts 的群聊取值为 "chatroom"（不是 "group"）；
        # openim 为企业微信/客服联系人，按一对一私聊归入 friend
        for c_kind, item_kind in (("chatroom", "group"), ("friend", "friend"),
                                  ("openim", "friend")):
            try:
                contacts = self._wxdb.get_contacts(kind=c_kind, limit=0)
            except Exception as exc:
                logger.warning("读取联系人（%s）失败：%s", c_kind, exc)
                contacts = []
            for c in contacts:
                username = str(c.get("username") or "")
                if not username or username in seen or username.startswith("gh_") \
                        or _is_system_account(username):
                    continue
                remark = str(c.get("remark") or "").strip()
                nick = str(c.get("nick_name") or "").strip()
                corp = corp_map.get(username, "")
                # 用户手动备注优先（微信里备注会盖掉「昵称@企业」）；
                # 无备注且能拿到企业简称时才拼后缀
                name = remark or self._with_corp(nick or username, corp)
                items.append({"username": username,
                              "display_name": name, "kind": item_kind,
                              "openim": c_kind == "openim"
                                        or self.is_openim(username),
                              "corp": corp})
                seen.add(username)
        logger.info("联系人列表构建完成：%d 条（群/好友/企业微信合并去重后，"
                    "其中 %d 个企业微信联系人有企业名）",
                    len(items), len(corp_map))
        return items

    def _openim_corp_map(self) -> dict[str, str]:
        """企业微信联系人 → 企业简称（读取失败返回空字典，不影响列表构建）。"""
        try:
            return self._wxdb.get_openim_corp_names()
        except Exception as exc:
            logger.warning("读取企业微信企业名失败：%s", exc)
            return {}

    @staticmethod
    def _with_corp(name: str, corp: str) -> str:
        """按微信端习惯拼「昵称@企业简称」（已含后缀或无企业名时原样返回）。"""
        if not corp or not name or "@" in name:
            return name
        return f"{name}@{corp}"

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
            [{username, display_name, kind, openim}]
        """
        kind = kind if kind in ("group", "friend", "all") else "all"
        limit = max(1, min(limit, 100))
        if self._wxdb is None:
            return []
        kw = (keyword or "").strip()
        items = self._get_cached_contact_list()
        if not kw:
            # 默认列表（打开面板时的首屏）：不用排序，按缓存顺序取够即止，
            # 避免对 3 万条全量排序
            out: list[dict] = []
            for item in items:
                if kind != "all" and item["kind"] != kind:
                    continue
                out.append(dict(item))
                if len(out) >= limit:
                    break
            return out
        # 有关键词：先收集全部命中，按匹配质量排序后再按 limit 截断。
        # 好友基数很大（本机约 3 万条），若边收集边截断，会出现
        # 「名字完全一致的联系人」被一堆昵称含同字的账号挤出前 50 条。
        hits: list[tuple[int, int, dict]] = []
        for order, item in enumerate(items):
            if kind != "all" and item["kind"] != kind:
                continue
            name = item["display_name"]
            if kw not in name and kw not in item["username"]:
                continue
            hits.append((self._match_rank(name, item["username"], kw),
                         order, item))
        hits.sort(key=lambda t: (t[0], t[1]))
        return [dict(t[2]) for t in hits[:limit]]

    @staticmethod
    def _match_rank(name: str, username: str, kw: str) -> int:
        """搜索匹配质量排序权重：越小越靠前。

        0 = 显示名完全等于关键词（用户最可能想找的人）
        1 = 显示名以关键词开头
        2 = 显示名包含关键词
        3 = 仅 username 命中（wxid / openim id 等）

        企业微信联系人的显示名是「昵称@企业简称」，匹配时把 ``@`` 之后的
        企业名视作后缀：搜「邢不行」应命中原名并获得与普通好友相同的权重。
        """
        base = name.split("@", 1)[0] if "@" in name else name
        if name == kw or base == kw:
            return 0
        if name.startswith(kw) or base.startswith(kw):
            return 1
        if kw in name:
            return 2
        return 3 if kw in username else 4

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

    def _resolve_row_sender(self, username: str, content: str,
                            fallback_wxid: str,
                            sender_id: int = 0) -> tuple[str, str]:
        """历史消息的 (发送人显示名, 剥离前缀后的正文)。

        群聊与实时监听同一权威口径：正文首部「发送者ID:\\n」前缀优先
        （SenderName2Id 映射不可靠，反馈 6 群昵称错乱）。只有前缀能对上
        群成员（wxid 或群显示名）时才剥离，避免误伤「9:30 开会」这类正文；
        私聊发送人即会话对方，sender_id=2 为自己。
        """
        text = str(content or "")
        if not username.endswith("@chatroom"):
            if sender_id in (2, "2"):
                return "我", text
            name = self.get_display_name(fallback_wxid) if fallback_wxid else "我"
            return name or "我", text
        names = self._get_group_member_names(username)
        candidate = self.extract_content_sender(text)
        reverse = self._group_member_reverse_names(username) if candidate else {}
        resolved = ""
        if candidate:
            if candidate in names:
                resolved = candidate
            elif candidate in reverse:
                resolved = reverse[candidate]
        if resolved:
            display = self.resolve_group_member_display(username, resolved)
            clean = _SENDER_ID_STRICT_RE.sub("", text, count=1).lstrip("\n")
            return display or resolved, clean
        if fallback_wxid:
            display = self.resolve_group_member_display(username, fallback_wxid)
            return display or fallback_wxid, text
        return (candidate or "未知用户"), text

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
            mtype = str(m.get("type") or "")
            sender_id = int(m.get("sender_id") or 0)
            # 发送人权威解析 + 剥掉正文里的「发送者ID:」前缀（反馈 6/11）
            sender_name, clean_content = self._resolve_row_sender(
                username, str(m.get("content") or ""), sender_wxid, sender_id)
            out.append({
                "local_id": int(m.get("local_id") or 0),
                "type": mtype,
                "type_name": mtype,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "create_time": epoch_to_iso(m.get("create_time")),
                # 展示层友好化：链接/小程序/图片等 XML 转人可读短文本（反馈 3）
                "content": content_format.format_content(mtype, clean_content),
                "sort_seq": int(m.get("sort_seq") or 0),
            })
            if len(out) >= limit:
                break
        self._fill_voice_texts(username, out)
        return out, False

    def _fill_voice_texts(self, username: str, msgs: list[dict]) -> None:
        """用 voice_texts 缓存回填语音消息文本（**不在请求线程里转写**）。

        消息列表/商机上下文一次可能返回 200 条，其中若有 10 条语音，
        逐条现场识别要十几秒，会直接把列表接口拖崩。这里只做一次批量
        查询，命中缓存才替换 content，未命中的保持 ``[语音]`` 占位，
        真正的补转写交给分析线程（pipeline，可接受慢）。
        """
        voice_ids = [int(m.get("local_id") or 0) for m in msgs
                     if content_format.is_voice_type(str(m.get("type") or ""))]
        voice_ids = [i for i in voice_ids if i > 0]
        if not voice_ids:
            return
        try:
            cached = self._db.get_voice_texts(username, voice_ids)
        except Exception:  # 缓存读取失败只是少显示文字，不影响消息列表
            logger.exception("读取语音转写缓存失败（按未命中处理）")
            return
        if not cached:
            return
        for item in msgs:
            if not content_format.is_voice_type(str(item.get("type") or "")):
                continue
            row = cached.get(int(item.get("local_id") or 0))
            if not row:
                continue
            text = str(row.get("text") or "").strip()
            if text:
                item["content"] = content_format.voice_text_content(text)

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

    def get_latest_local_id(self, username: str) -> int:
        """会话最新一条消息的 local_id（0 = 读不到）。

        引用回复用它判断「用户要引用的就是最新一条」：是的话直接右键最新
        消息即可，不必拿文本去消息列表里匹配（图片/小程序消息没有可识别
        文字，匹配必然失败）。
        """
        if self._wxdb is None:
            return 0
        try:
            rows = self._wxdb.get_messages(username, limit=1)
        except Exception as exc:
            logger.debug("读取最新消息 local_id 失败（%s）：%s", username, exc)
            return 0
        if not rows:
            return 0
        try:
            return int(rows[0].get("local_id") or 0)
        except Exception:
            return 0

    def list_group_members(self, chatroom: str) -> List[dict]:
        """群成员列表（供前端「@群成员」下拉选择）。

        返回 ``[{"name": 群内显示名, "wxid": 成员ID, "is_owner": bool}]``，
        按显示名排序；非群会话返回空列表。显示名口径与消息发送人一致
        （好友备注 → 群名片 → wxid），因为 @ 弹层里显示的就是这个名字。
        """
        if not chatroom.endswith("@chatroom") or self._wxdb is None:
            return []
        names = self._get_group_member_names(chatroom)
        owner = ""
        try:
            for member in self._wxdb.get_group_members(chatroom):
                if member.get("is_owner"):
                    owner = str(member.get("username") or "")
                    break
        except Exception:
            owner = ""
        out = [{"name": display, "wxid": uid,
                "is_owner": uid == owner and bool(owner)}
               for uid, display in names.items() if uid and display]
        out.sort(key=lambda m: (not m["is_owner"], m["name"]))
        return out

    # ------------------------------------------------------------------
    # 图片消息解密（反馈 2：详情/上下文中的图片显示为真图）
    # ------------------------------------------------------------------
    def _get_media_downloader(self):
        """懒加载并复用 MediaDownloader（图片解密 / 语音下载共用）。

        首次调用会解密媒体库（本机约 13s），因此**不能**每条消息都新建；
        失败返回 None（由调用方转成用户可读原因）。
        """
        if self._media_downloader is not None:
            return self._media_downloader
        with self._media_lock:
            if self._media_downloader is not None:
                return self._media_downloader
            try:
                from wechatauto.media import MediaDownloader
            except Exception:
                logger.warning("wechatauto.media 不可用，图片/语音媒体功能降级")
                return None
            try:
                self._media_downloader = MediaDownloader(
                    self._wxdb, save_dir=str(IMG_CACHE_DIR))
            except Exception:
                logger.exception("创建 MediaDownloader 失败")
                return None
            return self._media_downloader

    def get_image_bytes(self, username: str, local_id: int) -> Optional[bytes]:
        """解密会话中指定图片消息，返回图片字节；失败返回 None。

        底层用 wechatauto.media.MediaDownloader 定位并解密 .dat
        （群聊原图未点开时只有缩略图缓存，自动回退缩略图）。解密结果
        由 MediaDownloader 落盘到 IMG_CACHE_DIR（持久缓存，避免重复
        解密；目录在 data/ 下已被 git 忽略）。

        仅接受可内嵌展示的格式（jpg/png/gif）；wxgf 转码失败等情况
        返回 None，由前端回退占位卡片。
        """
        if self._wxdb is None:
            return None
        downloader = self._get_media_downloader()
        if downloader is None:
            return None
        try:
            path = downloader.download_image(username, local_id)
        except Exception as exc:
            logger.warning("图片解密失败（%s local_id=%d）：%s",
                           username, local_id, exc)
            return None
        if not path:
            return None
        if not str(path).lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
            return None  # wxgf 等无法内嵌展示的格式
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    # ------------------------------------------------------------------
    # 语音消息离线转写（本地识别，音频绝不上传外部服务）
    # ------------------------------------------------------------------
    def transcribe_voice(self, username: str, local_id: int) -> tuple[str, str]:
        """把一条语音消息转成文字，返回 (文本, 失败原因)。

        流程：``voice_texts`` 缓存命中直接返回 → 否则从微信本地库取
        ``.silk`` → 本地解码 + SenseVoice 识别 → 成功结果写缓存。

        缓存是刚需：同一条语音会在商机上下文、消息列表、分析输入里被
        反复读到，每次重跑识别（0.5~2 秒、CPU 满载）会明显拖慢整条链路。
        失败结果**不写缓存**（可能是「微信还没下载该语音」这类可恢复状态，
        下次重试应能成功），也不影响调用方主流程。
        """
        try:
            local_id_int = int(local_id or 0)
        except (TypeError, ValueError):
            return "", "语音消息标识非法"
        username = str(username or "")
        if not username or local_id_int <= 0:
            return "", "语音消息标识非法"

        # 1) 缓存命中（只认有文本的行；失败结果不落缓存，见方法文档）
        cached = self._read_voice_text(username, local_id_int)
        if cached and str(cached.get("text") or "").strip():
            return str(cached.get("text") or "").strip(), ""

        # 2) 开关 / 依赖可用性
        try:
            enabled = self._db.get_setting(KEY_ASR_ENABLED, "1") != "0"
        except Exception:  # 设置读取失败按「开启」处理，由可用性检查兜底
            enabled = True
        if not enabled:
            return "", MSG_ASR_DISABLED
        transcriber = voice_asr.get_transcriber()
        ok, reason = transcriber.available()
        if not ok:
            return "", reason
        if self._wxdb is None:
            return "", MSG_DB_UNAVAILABLE_ERROR

        # 3) 取音频 → 本地识别
        silk_path = self._download_voice_silk(username, local_id_int)
        if not silk_path:
            return "", MSG_ASR_NO_VOICE
        started = time.monotonic()
        text, err = transcriber.transcribe(silk_path)
        wall_ms = int((time.monotonic() - started) * 1000)
        # duration_ms 只统计「解码 + 识别」本身：首次调用会把模型加载时间
        # （实测 2.5~11s）算进墙钟，直接落库会让耗时统计严重虚高。
        duration_ms = self._recognition_ms(transcriber, wall_ms)
        if not text:
            logger.info("语音转写失败（%s local_id=%d，耗时 %dms）：%s",
                        username, local_id_int, wall_ms, err)
            return "", err or "语音转写失败"
        self._write_voice_text(username, local_id_int, text=text,
                              status="ok", duration_ms=duration_ms)
        logger.info("语音转写成功（%s local_id=%d，%d 字，识别耗时 %dms，"
                    "整体 %dms）", username, local_id_int, len(text),
                    duration_ms, wall_ms)
        return text, ""

    @staticmethod
    def _recognition_ms(transcriber, wall_ms: int) -> int:
        """本条语音的「识别耗时」（毫秒）：SILK 解码 + decode_stream。

        优先取转写器的分段计时（不含首次模型加载）；转写器被替身替换或
        未产生分段计时时，退回墙钟耗时（此时也没有模型加载可扣）。
        """
        getter = getattr(transcriber, "last_timing", None)
        if callable(getter):
            try:
                timing = getter() or {}
            except Exception:  # 分段计时读取失败：退回墙钟
                logger.exception("读取语音分段耗时失败（按墙钟处理）")
                timing = {}
            if timing:
                return max(0, int(timing.get("decode_ms") or 0)
                           + int(timing.get("recognize_ms") or 0))
        return max(0, wall_ms)

    def _read_voice_text(self, username: str,
                         local_id: int) -> Optional[dict]:
        """读语音转写缓存（异常降级为未命中，不影响主流程）。"""
        try:
            return self._db.get_voice_text(username, local_id)
        except Exception:
            logger.exception("读取语音转写缓存失败（按未命中处理）")
            return None

    def _write_voice_text(self, username: str, local_id: int, *, text: str,
                          status: str, duration_ms: int = 0) -> None:
        """写语音转写缓存（异常只记日志：缓存失败不该让转写结果丢失）。"""
        try:
            self._db.save_voice_text(username, local_id, text=text,
                                     status=status, duration_ms=duration_ms)
        except Exception:
            logger.exception("写入语音转写缓存失败（已忽略）")

    def _download_voice_silk(self, username: str,
                             local_id: int) -> Optional[str]:
        """从微信本地库取出语音原始 SILK 文件；失败返回 None。

        wechatauto 的 ``download_voice`` 只在 ``media_*.db`` 的 VoiceInfo
        里找 voice_data（微信未播放/未下载的语音在这里就是空的），因此
        返回 None 属正常情况，由调用方转成「未找到语音数据」提示。
        """
        if self._wxdb is None:
            return None
        downloader = self._get_media_downloader()
        if downloader is None:
            return None
        try:
            VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = downloader.download_voice(username, local_id,
                                             save_dir=str(VOICE_CACHE_DIR))
        except Exception as exc:
            logger.warning("取语音数据失败（%s local_id=%d）：%s",
                           username, local_id, exc)
            return None
        if not path:
            return None
        return str(path)

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

    def cache_status(self) -> list:
        """各消息分片解密缓存的健康度（只读，供 /api/status 展示）。

        数据来自 wechatauto.db.WeChatDB.cache_status()：源库持续写入但缓存
        长期未能成功更新时，轮询读陈旧缓存会恒返回空且没有任何信号——这里
        把 stale_seconds / last_error 暴露到状态接口，供前端与运维发现
        "消息停摆"类故障（对应线上 3 小时无新商机的排查缺口）。
        任何异常都降级为空列表，绝不影响 /api/status 本身。
        """
        wxdb = self._wxdb
        if wxdb is None:
            return []
        getter = getattr(wxdb, "cache_status", None)
        if not callable(getter):
            return []
        try:
            items = getter() or []
        except Exception:  # 防御：状态读取失败不影响主接口
            logger.exception("读取解密缓存健康度失败（按空列表处理）")
            return []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            stale = item.get("stale_seconds")
            result.append({
                "rel": str(item.get("rel") or ""),
                "stale_seconds": (None if stale is None else float(stale)),
                "last_ok_at": str(item.get("last_ok_at") or ""),
                "last_error": str(item.get("last_error") or ""),
                "fail_streak": int(item.get("fail_streak") or 0),
                "rebuilding": bool(item.get("rebuilding")),
            })
        return result

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

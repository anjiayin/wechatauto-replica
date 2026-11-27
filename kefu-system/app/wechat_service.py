# -*- coding: utf-8 -*-
"""微信接入层服务：WeChatDB/Listener 单例封装。

职责（技术方案 T02）：
- 懒加载 wechatauto（导入失败/非 Windows/缺依赖时友好降级，不崩溃）；
- 启停 Listener、注册/注销监控会话回调；
- watermark 定期（30s）与退出时刷回 watermarks 表；
- 会话搜索 / 历史消息查询（供 API 复用）；
- 图片识别任务队列（监听线程只入队，独立 worker 跑视觉模型）；
- 状态查询（/api/status 数据源）。

wechatauto 只 import 调用，不做任何修改（硬约束）。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from app import db as dbmod
from app import content_format
from app.content_format import SENDER_ID_STRICT_RE
from app import vision
from app import voice_asr
from app.crypto import decrypt_api_key
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
    VISION_QUEUE_MAX,
    VISION_QUEUE_WARN,
    VISION_WORKERS,
    VOICE_CACHE_DIR,
    WATERMARK_FLUSH_SEC,
    KEY_ASR_ENABLED,
    KEY_ASR_MAX_SECONDS,
    KEY_ANALYSIS_FILTER_SELF,
    KEY_RUNTIME_CACHE_DIR,
    KEY_RUNTIME_WECHAT_ACCOUNT,
    KEY_RUNTIME_WECHAT_ACCOUNT_NICKS,
    KEY_RUNTIME_WM_SAVED_AT,
    CACHE_DIR_DEFAULT,
    dir_usage,
    resolve_cache_dir,
)
from app.db import Database, epoch_to_iso
from app.image_retry import ImageRetryQueue
from app.voice_retry import VoiceRetryQueue

logger = logging.getLogger(__name__)

# 企业微信「昵称@企业简称」所需的企业简称表缓存时长（秒）。
# 企业简称几乎不变（只有新增企业微信联系人才会变），10 分钟足够新；
# 读失败时另按 30 秒短缓存重试，见 WeChatService.openim_corp_map。
CORP_MAP_CACHE_TTL_SEC = 600.0
# 当前监控账号 ↔ Weixin.exe PID 绑定的缓存时长（秒）。双开时确认归属要读
# 进程内存里的主密钥，开销较大；状态接口每 5 秒轮询，必须缓存。
ACCOUNT_BINDING_TTL_SEC = 15.0

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
# 发送者提取/剥离用严格正则统一收敛到 content_format.SENDER_ID_STRICT_RE
# （与 Aggregator 剥前缀共享同一口径，批次 5 需求 ①）：
# - ID 字符集排除 "/"（URL 路径字符）；
# - 冒号后紧跟 "//" 视为 URL scheme（如 http://...），不提取（BUG-3）；
# - 提取时先按换行切分仅取首行，避免多行正文匹配失败回退到不可靠
#   sender_username 映射（BUG-1，见 extract_content_sender）。
# 合并文本行首的 "[HH:MM] " 时间前缀（Aggregator.merged_text 系统加的）
_TIME_PREFIX_RE = re.compile(r"^\[\d{1,2}:\d{2}\]\s*")
# 群聊消息里的 @人微信 ID token（AI 输入还原显示名用，需求 ④）：
# @wxid_xxx / @xxx@chatroom / @xxx@openim
_AT_ID_TOKEN_RE = re.compile(
    r"@(?:wxid_[0-9A-Za-z_-]{5,}"
    r"|[0-9A-Za-z_.-]{1,64}@(?:chatroom|openim))")

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
        # 本次 start() 的 epoch 水位（秒）；0 = 尚未启动/测试直调，不启用闸门。
        # Listener 的 sort_seq watermark 只记「上次刷回位置」，客户端停机期间
        # 收到的消息 sort_seq 更大，重启后会被当新消息全部回放；这里用启动
        # 时刻做 create_time 闸门，把停机积压挡在聚合/AI 队列之外。
        self._session_start_ts: float = 0.0
        self._flush_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._name_cache: dict[str, str] = {}   # username → display name 缓存
        self._name_cache_lock = threading.Lock()
        # username → 微信号(alias) 缓存。用户要求界面上原 wxid 的位置统一
        # 显示微信里可见的微信号；alias 为空时回退备注/昵称（见 get_wechat_id）。
        self._alias_cache: dict[str, str] = {}
        self._alias_cache_lock = threading.Lock()
        # 企业微信联系人 → 企业简称（整张表缓存：读 contact.db 解密很慢，
        # 商机/日志一页几十行逐行读会把接口拖到秒级）
        self._corp_map_cache: tuple[float, dict, bool] = (0.0, None, False)
        self._corp_map_lock = threading.Lock()
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
        # 会话最后消息时间缓存（username → last_timestamp，搜索按近期活跃
        # 排序用；与联系人列表缓存同 TTL，构建时一并失效）
        self._session_times: dict[str, int] = {}
        self._session_times_ts: float = 0.0
        # 当前登录账号 wxid（过滤自己发送的消息用，懒加载缓存）
        self._self_wxid: str = ""
        # 当前监控账号 ↔ 微信实例 PID 绑定（双开防发错账号）
        self._binding_lock = threading.Lock()
        self._binding_cache: Optional[tuple[float, dict]] = None
        # 媒体下载器懒加载单例（MediaDownloader 首次使用会解密媒体库，约 13s，
        # 不能每条消息/每张图片都新建一个）。图片与语音共用同一实例。
        self._media_downloader = None
        self._media_lock = threading.Lock()
        # 图片密钥主动捕获（1.2.7「重新检查」修复）：后台线程做一次带墙钟
        # 预算的只读内存扫描；防并发重入；状态随 image_key_status() 回显，
        # 供设置页展示「正在扫描/上次结果」。绝不放在 HTTP 请求线程里跑。
        self._image_key_recheck_lock = threading.Lock()
        self._image_key_recheck: dict = {
            "running": False,      # 是否正在后台扫描
            "last_ok": None,       # None=从未跑过 / True=捕获成功 / False=未捕获
            "last_at": "",         # 最近一次扫描结束时刻（ISO）
            "last_error": "",      # 失败原因（用户可读）
        }
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
        # 图片识别任务队列（批次 6 需求 5 异步化）：视觉识别单张实测 13.6s、
        # 超时上限 60s，在 Listener 回调线程里同步跑会把后续文本消息的入库
        # 推迟几十秒。监听线程只做「开关判断 + 入队」，取图/识别/落库/聚合
        # 全部由 VISION_WORKERS 个 worker 线程完成（有界队列 + 满则跳过）。
        self._vision_queue: "queue.Queue" = queue.Queue(
            maxsize=VISION_QUEUE_MAX)
        self._vision_threads: List[threading.Thread] = []
        self._vision_lock = threading.Lock()
        self._vision_warn_ts: float = 0.0   # 积压告警限频时间戳（monotonic）
        self._vision_ok: int = 0            # 已得出结论的任务数
        self._vision_fail: int = 0          # 识别失败（坏图/网关异常）任务数
        # 图片「不完整」延迟重试队列（批次 7 需求 2）：微信原图要等客户端下载
        # 完才落库，首次取图经常拿到「不完整」；批次 6 只落 skipped 日志，这张
        # 图就永久漏识别。现在按退避序列自动补识别（见 app/image_retry.py）。
        # on_drop：队列满丢最旧任务时落一条可见日志（否则用户完全不知道
        # 哪张图被丢了，批次 7 QA-P3-2）。
        self._image_retry = ImageRetryQueue(
            handler=self._retry_image_task,
            on_drop=self._on_image_retry_dropped,
            on_give_up=self._on_image_retry_given_up)

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
        # 解密缓存/密钥按账号隔离在 <缓存根>/<账号> 子目录，属于配置目录
        # 即视为已在用，避免设置页误报「已保存但未生效」。
        active = False
        if active_dir:
            try:
                child = os.path.normcase(os.path.abspath(active_dir))
                root = os.path.normcase(os.path.abspath(str(path)))
                active = child == root or child.startswith(root + os.sep)
            except (OSError, ValueError):
                active = False
        return {
            "dir": str(path),
            "default_dir": str(CACHE_DIR_DEFAULT),
            "configured": configured,
            "is_default": not str(configured).strip(),
            "size_bytes": size,
            "file_count": count,
            "active_dir": active_dir,
            "active": active,
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

    def _adopt_legacy_keys(self, target: Path,
                           base: Optional[Path] = None) -> None:
        """把旧的密钥缓存搬到目标目录（仅当目标缺失）。

        目标目录现在是**按账号隔离**的子目录（``<缓存根>/<账号>``）：
        keys.json 与解密缓存都只按库相对路径命名，共用一个目录会让换号
        后的查询直接命中上一个账号的解密缓存。首次升级到隔离布局时，
        依次尝试两个来源，避免无谓地重新扫描微信进程内存：

        1. 旧共享缓存根目录（升级前 workdir 就是它）；
        2. 旧 %TEMP% 目录（历史版本），优先同名账号子目录，其次最新修改。

        keys.json 里的每条密钥都会被 WeChatDB 按目标账号的库做页 1 HMAC
        校验，不匹配即丢弃并重新提取，所以复制来源不同也不会用错密钥。
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
            best_key: Optional[tuple] = None
            if base is not None and target.resolve() != base.resolve():
                src = base / name
                try:
                    if src.is_file():
                        # 优先级最高：共享缓存根就是升级前的 workdir，
                        # 其中的密钥属于最近在用的账号
                        best, best_key = src, (3, src.stat().st_mtime)
                except OSError:
                    best, best_key = None, None
            for d in self._legacy_cache_dirs():
                src = d / name
                try:
                    if not src.is_file():
                        continue
                    # 同名账号目录优先（旧 TEMP 布局本身就按账号分目录）
                    key = (2 if d.name == target.name else 1,
                           src.stat().st_mtime)
                except OSError:
                    continue
                if best_key is None or key > best_key:
                    best, best_key = src, key
            if best is None:
                continue
            try:
                shutil.copy2(best, dst)
                logger.info("已迁移密钥缓存：%s → %s", best, dst)
            except OSError as exc:
                logger.warning("密钥缓存迁移失败（%s）：%s", best, exc)

    # ------------------------------------------------------------------
    # 监控微信账号（双开时必须明确「读哪个账号、点哪个实例」）
    # ------------------------------------------------------------------
    def configured_account(self) -> str:
        """用户指定的监控账号目录名（空 = 自动选最近活跃账号）。"""
        return str(self._db.get_setting(KEY_RUNTIME_WECHAT_ACCOUNT, "") or "").strip()

    def effective_account(self) -> str:
        """当前真正在监控的账号目录名。"""
        wxdb = self._wxdb
        active = (str(getattr(wxdb, "account", "") or "").strip()
                  if wxdb is not None else "")
        return active or self.configured_account()

    def account_list(self) -> list[dict]:
        """本机检测到的微信账号目录（供设置页选择监控账号）。"""
        try:
            from wechatauto.db import list_accounts
            items = list(list_accounts() or [])
        except Exception as exc:
            logger.warning("扫描微信账号目录失败：%s", exc)
            return []
        active = self.effective_account()
        configured = self.configured_account()
        nicks = self._load_account_nicks()
        # 当前账号昵称以最近一次绑定计算为准（可能刚登录/刚改昵称）
        with self._binding_lock:
            cached = self._binding_cache
        if cached is not None:
            cached_info = cached[1]
            cached_account = str(cached_info.get("account") or "")
            cached_nick = str(cached_info.get("nick_name") or "")
            if cached_account and cached_nick:
                nicks.setdefault(cached_account, cached_nick)
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            account = str(item.get("account") or "")
            item["account"] = account
            item["active"] = account == active
            item["configured"] = account == configured
            item["nick_name"] = nicks.get(account, "")
            out.append(item)
        return out

    def _load_account_nicks(self) -> dict:
        """已知账号昵称表（账号目录名 → 昵称）；损坏/为空返回空表。"""
        raw = ""
        try:
            raw = self._db.get_setting(KEY_RUNTIME_WECHAT_ACCOUNT_NICKS, "")
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if k and v}

    def _remember_account_nick(self, account: str, nick_name: str) -> None:
        """记住「账号目录名 → 昵称」：昵称需解密 contact.db 才能读到，
        未登录的账号无法实时获取，持久化后设置页可显示名称而非 wxid。"""
        account = str(account or "").strip()
        nick_name = str(nick_name or "").strip()
        if not account or not nick_name:
            return
        known = self._load_account_nicks()
        if known.get(account) == nick_name:
            return
        known[account] = nick_name
        try:
            self._db.set_setting(
                KEY_RUNTIME_WECHAT_ACCOUNT_NICKS,
                json.dumps(known, ensure_ascii=False, sort_keys=True))
        except Exception:
            logger.debug("记录微信账号昵称失败（忽略）", exc_info=True)

    def account_binding(self, force: bool = False) -> dict:
        """当前监控账号 ↔ Weixin.exe 实例绑定（双开防发错账号）。

        返回字段：account/wxid/nick_name/pid/instance_count/mapped/pinned/
        active/reason。``pid=0`` 且 ``instance_count>1`` 时调用方必须
        fail-closed（绝不猜一个窗口发送）。
        """
        now = time.time()
        with self._binding_lock:
            cached = self._binding_cache
            if (not force and cached is not None
                    and now - cached[0] < ACCOUNT_BINDING_TTL_SEC):
                return dict(cached[1])
        info = self._compute_account_binding()
        with self._binding_lock:
            self._binding_cache = (time.time(), dict(info))
        return info

    def _compute_account_binding(self) -> dict:
        """实际计算账号身份与实例 PID（可能读进程内存，结果应缓存）。"""
        configured = self.configured_account()
        wxdb = self._wxdb
        active = (str(getattr(wxdb, "account", "") or "").strip()
                  if wxdb is not None else "")
        account = active or configured
        wxid = ""
        if wxdb is not None and active:
            try:
                wxid = str(getattr(wxdb, "wxid", "") or "")
            except Exception:
                wxid = ""
        if not wxid and account:
            wxid = re.sub(r"_\w{4}$", "", account)
        nick_name = ""
        if wxdb is not None and active:
            try:
                self_info = wxdb.get_self_info() or {}
                nick_name = str(self_info.get("nick_name") or "")
            except Exception:
                logger.debug("读取监控账号昵称失败（忽略）", exc_info=True)
        if not nick_name and account:
            # 降级（未打开库）时从已保存昵称表回退，顶栏仍显示「昵称」而非 wxid
            try:
                nick_name = str(self._load_account_nicks().get(account, "") or "")
            except Exception:
                nick_name = ""
        if account and nick_name:
            self._remember_account_nick(account, nick_name)
        try:
            from wechatauto.db import enumerate_weixin_pids
            pids = enumerate_weixin_pids()
        except Exception:
            pids = []
        pid = 0
        if wxdb is not None:
            try:
                # 指定了账号（pinned）时即使单实例也校验，防「所选账号未登录、
                # 实际开着另一个微信」时把新账号的消息回给旧账号联系人。
                pid = int(wxdb.find_account_pid(
                    pids, strict=bool(configured)) or 0)
            except Exception:
                logger.debug("定位监控账号微信实例失败", exc_info=True)
                pid = 0
        reason = ""
        if not pids:
            reason = "未检测到正在运行的微信（请先登录监控账号）"
        elif len(pids) > 1 and not pid:
            reason = ("检测到多个微信实例，但无法确认哪个属于当前监控账号；"
                      "已暂停自动回复以免发到其它账号，请重启微信或在"
                      "「设置 → 数据与账单 → 监控微信账号」重新选择")
        elif wxdb is None:
            reason = "微信数据库未就绪，无法确认监控账号实例"
        elif configured and not pid:
            reason = (f"当前登录的微信不是所选监控账号（{configured}）；"
                      "已暂停自动回复以免发错账号，请在设置中重新选择或登录"
                      "该账号后重试")
        return {
            "account": account,
            "wxid": wxid,
            "nick_name": nick_name,
            "pid": pid,
            "instance_count": len(pids),
            "mapped": bool(pid),
            "pinned": bool(configured),
            "active": bool(wxdb is not None and self._available),
            "reason": reason,
        }

    def switch_account(self, account: str) -> dict:
        """切换监控账号：只重开 Listener，不重启发送/识别 worker。

        Args:
            account: 账号目录名；空串 = 恢复自动选择。

        Returns:
            {"ok": bool, "error": str, "binding": dict}
        """
        account = str(account or "").strip()
        if account:
            known = {str(a.get("account") or "") for a in self.account_list()}
            if account not in known:
                return {"ok": False,
                        "error": f"未找到微信账号目录：{account}",
                        "binding": self.account_binding(force=True)}
        with self._listener_lock:
            old = self._listener
            self._listener = None
        if old is not None:
            try:
                old.stop()
            except Exception:
                logger.exception("切换监控账号时停止旧监听异常")
            try:
                marks = old.watermark
                if marks:
                    self._db.save_watermarks(marks)
                    self._db.set_setting(KEY_RUNTIME_WM_SAVED_AT,
                                         dbmod.now_iso())
            except Exception:
                logger.exception("切换监控账号时刷回 watermark 失败")
        self._db.set_setting(KEY_RUNTIME_WECHAT_ACCOUNT, account)
        self._reset_account_caches()
        with self._listener_lock:
            ok = self._open_listener_locked(account)
        if ok:
            self._ensure_flush_thread()
        binding = self.account_binding(force=True)
        logger.info("监控微信账号已切换为：%s（监听%s）",
                    account or "自动", "已启动" if ok else "启动失败")
        return {"ok": ok,
                "error": "" if ok else (self._reason or "切换监控账号失败"),
                "binding": binding}

    def _reset_account_caches(self) -> None:
        """账号切换/换库后清空所有与账号绑定的缓存。"""
        with self._name_cache_lock:
            self._name_cache.clear()
        with self._alias_cache_lock:
            self._alias_cache.clear()
        with self._corp_map_lock:
            self._corp_map_cache = (0.0, None, False)
        with self._contact_cache_lock:
            self._contact_cache = []
            self._contact_cache_ts = 0.0
            self._session_times = {}
            self._session_times_ts = 0.0
        with self._group_members_lock:
            self._group_members_cache.clear()
        self._self_wxid = ""
        with self._media_lock:
            self._media_downloader = None
        with self._binding_lock:
            self._binding_cache = None

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def start(self) -> None:
        """拉起微信监听（失败降级，不抛异常）。"""
        # 补转写队列独立于微信监听可用性：即使微信库暂时不可用也先跑起来，
        # 任务失败会自然耗尽重试（不占用监听线程）。
        self._voice_retry.start()
        # 图片识别 worker 同理：与监听生命周期解耦，先起线程再拉监听，
        # 避免监听刚启动就有图片消息却没人消费
        self._start_vision_workers()
        # 补识别队列放在 worker 之后：worker 才是「发现图片不完整」的一方，
        # 队列消费者后起不会漏任务
        self._image_retry.start()
        with self._listener_lock:
            if self._listener is not None:
                return
            self._open_listener_locked(self.configured_account())
        if self._listener is not None:
            self._ensure_flush_thread()

    def _open_listener_locked(self, account: str = "") -> bool:
        """（重）建 WeChatDB + Listener；调用方必须持 ``_listener_lock``。

        Args:
            account: 要监控的账号目录名；空 = 自动选最近活跃账号。
                指定账号时必须真实存在（含 db_storage），否则给出可操作
                的降级原因，绝不悄悄回退到另一个账号。

        Returns:
            True = 监听已启动；False = 已降级（原因见 ``degraded_reason``）。
        """
        account = str(account or "").strip()
        try:
            from wechatauto.db import (Listener, WeChatDB, auto_detect_db_dir,
                                       list_accounts)
        except Exception as exc:
            self._degrade(f"微信组件加载失败：{exc}")
            return False
        try:
            # 缓存目录可配置（默认 kefu-system/data/wechat_cache）：
            # 不再落在 %TEMP% 下，避免被系统清理后全量重新解密。
            cache_dir = self.configured_cache_dir()
            if account:
                db_dir = auto_detect_db_dir()
                target = os.path.join(db_dir or "", account, "db_storage")
                if not db_dir or not os.path.isdir(target):
                    self._wxdb = None
                    self._reset_account_caches()
                    self._degrade(
                        f"配置的监控微信账号不存在或未登录：{account}。"
                        "请在「设置 → 数据与账单 → 监控微信账号」重新选择")
                    return False
                # 所选账号未登录时快速失败：否则 WeChatDB 构造会扫描全部
                # 微信进程内存找该账号的密钥（约 110 秒）且注定失败。
                # 只在句柄给出「明确不是该账号」的证据时拦截。
                if self._account_instance_absent(account, db_dir):
                    self._wxdb = None
                    self._reset_account_caches()
                    self._degrade(
                        f"所选监控账号「{self._account_display(account)}」当前未登录："
                        "正在运行的微信不是该账号，无法读取该账号的数据。"
                        "请登录该账号后重试，或在「设置 → 数据与账单 → "
                        "监控微信账号」切换到正在运行的账号")
                    return False
            # 解密缓存与密钥必须按账号隔离到 <缓存根>/<账号>：
            # keys.json 与 .pages/.stamp 只按库相对路径命名，共用一个
            # 目录会让换号后的查询直接命中上一个账号的解密缓存
            # （实测：切到未登录账号仍显示原账号的联系人/群列表）。
            workdir = cache_dir
            resolved = account
            if not resolved:
                try:
                    accounts = list_accounts()
                    resolved = (str(accounts[0].get("account") or "")
                                if accounts else "")
                except Exception:
                    resolved = ""
            if resolved:
                workdir = cache_dir / resolved
            self._adopt_legacy_keys(workdir, cache_dir)
            self._wxdb = WeChatDB(workdir=str(workdir),
                                  account=account or None)
            # 换了 wxdb 实例：账号相关的所有缓存都必须丢弃
            self._reset_account_caches()
            logger.info("微信数据库缓存目录：%s（%.1f MB，账号 %s）",
                        workdir, self._dir_size_mb(workdir),
                        getattr(self._wxdb, "account", "") or "自动")
        except Exception as exc:
            self._wxdb = None
            self._reset_account_caches()
            self._degrade(f"微信数据库不可用（微信可能未登录）：{exc}")
            return False
        try:
            # 记录本次启动时刻（epoch 秒）作为时间闸门下界。必须早于
            # watermark 推进：推进与 listener.start() 之间到达的真新消息
            # create_time >= 本水位，不会被误杀。
            self._session_start_ts = time.time()
            marks = self._db.load_watermarks()
            # 治本：把已启用会话的 watermark 推进到当前最新 sort_seq，
            # 让 Listener 从「最新」开始轮询，根本不回放停机积压。
            # 推进发生在构造 Listener 之前（而非 add_listener 之后），
            # 因为 add_listener 只在 user 不在 watermark 时赋值，绝不会
            # 覆盖这里写入的值（见 wechatauto/db.py:3299）。
            advanced_marks = self._advance_start_watermarks(marks)
            listener = Listener(self._wxdb, interval=1.0,
                                watermark=advanced_marks or None)
            for username in self._db.list_enabled_target_usernames():
                listener.add_listener(username, self._on_message)
            listener.start()
            self._listener = listener
            self._available = True
            self._reason = ""
            logger.info("微信监听已启动（账号 %s，监控会话 %d 个，加载 watermark "
                        "%d 条，推进后水位共 %d 条；启动水位=%.0f）",
                        getattr(self._wxdb, "account", "") or "自动",
                        len(self._db.list_enabled_target_usernames()),
                        len(marks), len(advanced_marks),
                        self._session_start_ts)
            return True
        except Exception as exc:
            self._listener = None
            self._degrade(f"微信监听启动失败：{exc}")
            return False

    @staticmethod
    def _account_instance_absent(account: str, db_dir: str) -> bool:
        """运行中的微信实例都明确不属于 account 时返回 True。

        只用进程打开文件句柄这一强证据（:func:`pid_account_match`）：
        任一实例确认属于该账号、或句柄无法读取（权限受限/微信未运行）
        都返回 False——宁可不拦，避免误伤正常场景。
        """
        try:
            from wechatauto.db import enumerate_weixin_pids, pid_account_match
            pids = enumerate_weixin_pids()
        except Exception:
            return False
        if not pids:
            return False
        account_dir = os.path.join(db_dir, account)
        evidence: List[Optional[bool]] = []
        for pid in pids:
            try:
                evidence.append(pid_account_match(pid, db_dir, account,
                                                  account_dir))
            except Exception:
                evidence.append(None)
        return bool(evidence) and all(v is False for v in evidence)

    def _account_display(self, account: str) -> str:
        """账号目录名 → 用户可读名称（已知昵称优先，否则去后缀 wxid）。"""
        account = str(account or "").strip()
        nick = str(self._load_account_nicks().get(account, "") or "").strip()
        if nick:
            return nick
        return re.sub(r"_\w{4}$", "", account)

    def _ensure_flush_thread(self) -> None:
        """确保 watermark 定期刷回线程在跑（账号切换时不重复启动）。"""
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return
        self._stop.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="kefu-wm-flush", daemon=True)
        self._flush_thread.start()

    def _advance_start_watermarks(self,
                                  marks: Optional[dict]) -> dict[str, int]:
        """把每个已启用会话的 watermark 推进到「当前最新 sort_seq」。

        wechatauto 的 ``Listener`` 用持久化的 sort_seq watermark 判定新消息。
        客户端停机期间微信仍在收消息，这些消息的 sort_seq > 上次退出时刷回的
        watermark，重启后会被 Listener 当「新消息」全部回放。这里在构造
        Listener 之前把水位推到各会话当前最新 sort_seq，使轮询从最新位置开始，
        从源头避免停机积压进入 ``_on_message``（时间闸门是第二道兜底）。

        读失败（微信库正忙/会话无消息）时保留传入的持久化水位，不擅自写 0：
        写 0 反而会导致 Listener 从头回放全部历史。

        Args:
            marks: ``load_watermarks()`` 返回的持久化水位；None/空字典均可。

        Returns:
            推进后的水位字典（不修改入参）。
        """
        advanced = dict(marks or {})
        if self._wxdb is None or self._db is None:
            return advanced
        try:
            usernames = self._db.list_enabled_target_usernames()
        except Exception:
            logger.debug("读取已启用监控会话失败（跳过 watermark 推进）：",
                         exc_info=True)
            return advanced
        for username in usernames:
            try:
                # get_messages 按 sort_seq DESC 跨分片归并，limit=1 即最新一条
                latest = self._wxdb.get_messages(username, limit=1)
            except Exception as exc:
                logger.debug("读取最新消息推进 watermark 失败"
                             "（沿用持久化水位）：%s：%s", username, exc)
                continue
            if not latest:
                continue
            seq = latest[0].get("sort_seq") if isinstance(latest[0], dict) \
                else None
            if isinstance(seq, (int, float)) and not isinstance(seq, bool):
                advanced[username] = int(seq)
        return advanced

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
        # 先停监听再停 worker：停止过程中不会再有新任务入队。worker 会把
        # 手头这一张跑完（视觉调用最长 60s，join 只等 5s，剩下交给 daemon
        # 线程随进程退出，不阻塞退出流程）
        self._stop_vision_workers()
        # 补识别队列最后停：worker 在上一步可能刚把「不完整」的图片排进来
        try:
            self._image_retry.stop()
        except Exception:
            logger.exception("停止图片补识别队列异常（已忽略）")
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
    def _is_before_monitor_since(self, username: str, msg: dict) -> bool:
        """消息是否早于该会话的「有效水位」（应丢弃）。

        有效水位 = max(配置完成时刻, 本次启动时刻)，两道闸门都要满足才放行：

        1. ``monitor_targets.monitor_since``（配置完成时写入的 epoch 秒）：
           兜底 Listener 因 watermark 过期/回退 0 而回放的多年历史消息；
        2. ``self._session_start_ts``（本次 ``start()`` 的 epoch 秒）：兜底
           **客户端停机期间**收到的消息。这类消息 create_time 晚于配置时刻，
           第 1 道闸门挡不住；但 sort_seq 大于上次刷回的 watermark，重启后
           会被 Listener 当新消息回放，必须靠启动水位拦截。

        两道闸门都在 skipped 日志/聚合窗口之前生效，不占处理队列。

        仅在「有效水位 > 0 + 消息时间有效且早于有效水位」时返回 True；
        未启动（测试直调）、存量水位 0、时间缺失或非法一律放行，避免误伤
        正常消息与存量行为。

        Args:
            username: 消息所属会话 username。
            msg: Listener 原始消息 dict（读取 create_time）。

        Returns:
            True 表示该消息属于配置前/启动前历史，应静默丢弃。
        """
        if not username:
            return False
        since = 0.0
        if self._db is not None:
            try:
                target = self._db.get_target_by_username(username)
            except Exception:
                # 测试替身/降级场景可能没有该 DAO 或读取失败；不限制。
                logger.debug("读取监控水位失败（按不过滤处理）：%s", username,
                             exc_info=True)
                target = None
            if target:
                try:
                    since = float(int(target.get("monitor_since") or 0))
                except (TypeError, ValueError):
                    since = 0.0
        # 叠加本次启动水位：没有该属性（老替身/降级）时按 0 处理，不误伤。
        try:
            session_start = float(getattr(self, "_session_start_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            session_start = 0.0
        if since <= 0 and session_start <= 0:
            return False
        raw_ts = msg.get("create_time")
        if raw_ts is None or raw_ts == "":
            return False
        try:
            epoch = float(raw_ts)
        except (TypeError, ValueError):
            return False
        if epoch <= 0:
            return False
        # 兼容 create_time 为毫秒的微信版本（秒级水位不要被放大 1000 倍）。
        if epoch > 1e11:
            epoch /= 1000.0
        # 配置水位用严格比较（沿用既有语义：早 1 秒也算配置前历史）。
        if since > 0 and epoch < since:
            logger.debug("配置前历史消息已过滤：会话=%s create_time=%s < 配置水位=%s",
                         username, raw_ts, since)
            return True
        # 启动水位按**秒级口径**比较：create_time 是微信消息库的整数秒，
        # start() 记录的是浮点秒；对启动时刻向下取整后严格比较，语义明确
        # 为「同一秒视为启动后」——既不误杀启动瞬间到达的真新消息，也不会
        # 放过分钟~小时级的停机积压。
        if session_start > 0 and epoch < int(session_start):
            logger.debug("停机期间历史消息已过滤：会话=%s create_time=%s < "
                         "启动水位=%s", username, raw_ts, session_start)
            return True
        return False

    def _on_message(self, msg: dict, listener=None) -> None:
        """Listener 新消息回调：非文本记 skipped 后丢弃，文本进合并窗口。

        语音消息特殊处理：先本地离线转写为文字，成功则改写 content 为
        ``[语音] 文本`` 并按**文本消息**正常进合并窗口参与商机分析；
        失败（开关关闭 / 模型未装 / 解码失败）保持原 skipped 行为，
        error 里写清原因，便于用户在分析日志里定位问题。
        """
        try:
            username = str(msg.get("username") or "")
            # 任务 #1：配置完成之前的历史消息不进入商机分析/处理队列。
            # 过滤必须在任何 skipped 日志、群名片学习、语音转写、图片入队
            # 之前完成，否则 Listener 回放历史仍会把队列打满。
            if self._is_before_monitor_since(username, msg):
                return
            mtype = str(msg.get("type") or "")
            kind = "group" if username.endswith("@chatroom") else "friend"
            sender_wxid, sender_name = self._resolve_sender(username, kind, msg)
            # 过滤自己发送的消息（批次 5 需求 ⑤，设置默认开启）：
            # 自己发的消息不进入商机分析，仅落一条 skipped 日志留痕，
            # 也不进入漏判修正候选（skipped 状态不参与修正示例挑选）。
            # 例外：与**自己**对话的会话（「给自己发消息」的 self 会话 /
            # 文件传输助手）里每条消息的 sender_id 都是 2，若一并过滤，
            # 这条通道会整体失效——而它恰恰是用户手工喂给系统的输入，
            # 本意就是让系统分析，所以不走「自己发的」过滤。
            if self._filter_self_enabled() \
                    and not self._is_self_chat(username) \
                    and self._is_self_message(kind, msg, sender_wxid):
                # 日志留痕文本与管线口径一致：群消息剥发送人前缀（需求 ①）
                content = str(msg.get("content") or "")
                if kind == "group":
                    content = content_format.strip_group_sender_prefix(content)
                self._db.insert_analysis_log(
                    session_username=username,
                    sender_wxid=sender_wxid,
                    msg_count=1,
                    input_text=content_format.format_content(
                        mtype, content)[:200],
                    status="skipped",
                    error="自己发送的消息已过滤（设置 → 分析判定）",
                )
                return
            # 群消息到达即学习「微信端实际显示的发送者名称」（群名片，
            # 批次 5 需求 ②）：session.db 的 last_sender_display_name 是
            # 微信客户端自己渲染的名字，落库后供显示名解析优先采用。
            if kind == "group" and sender_wxid:
                self._learn_group_card(username, sender_wxid)
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
                elif content_format.is_image_type(mtype):
                    # 图片消息（批次 6 需求 5）：视觉识别单张实测 10~15 秒，
                    # 同步跑会把 Listener 回调线程堵死，后续文本消息的入库
                    # 被推迟几十秒。这里只做「开关判断 + 入队」后立刻返回，
                    # 识别/落库/进聚合窗口全部交给独立 worker 线程池。
                    # 1.2.7 现场教训（openim 图片 40「零痕迹」丢失）：图片
                    # 链路「收到→入队→worker 开工→落库」各段此前全无日志，
                    # 任何一段卡住都表现为整体静默。这里在**收到**与入队
                    # 成功时各留一行 INFO（与 worker 侧「开始处理」呼应），
                    # 把链路分段 bracket 起来，下次再出现"有收到无处理"
                    # 可直接定位卡在哪一段。
                    try:
                        logger.info("收到图片消息（%s local_id=%s），进入识别链路",
                                    username, int(msg.get("local_id") or 0))
                    except (TypeError, ValueError):
                        logger.info("收到图片消息（%s），进入识别链路", username)
                    self._enqueue_image_message(
                        msg, username=username, kind=kind,
                        sender_wxid=sender_wxid, sender_name=sender_name,
                        mtype=mtype)
                    return
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
            # 企业微信联系人按微信 UI 习惯显示「昵称@企业简称」：只存昵称
            # 会让商机列表的「来源」看起来只有前半截，同名好友也无法区分
            session_name = self.display_name_with_corp(username)
            # 发送人已用可靠数据源解析（群=正文前缀 / 私聊=会话对方），
            # 覆盖 msg 中不可靠的 sender_username 供 Aggregator/管线透传
            msg["sender_username"] = sender_wxid
            msg["sender_display_name"] = sender_name
            self._aggregator.add(msg, session_name, kind)
        except Exception:
            logger.exception("处理新消息回调异常（已忽略）")

    # ------------------------------------------------------------------
    # 图片内容识别（批次 6 需求 5：图片里的车辆/配件 → 按卖货产出商机）
    #
    # 异步化：视觉识别单张实测 13.6s、超时上限 60s，在 Listener 回调线程里
    # 同步跑会把后续文本消息的入库推迟几十秒。监听线程只做「开关判断 +
    # put_nowait 入队」并立即返回（见 ``_enqueue_image_message``），取图、
    # 识别、落库、进聚合窗口全部由 ``kefu-vision-*`` worker 完成。
    # ------------------------------------------------------------------
    def _image_vision_settings(self) -> dict:
        """图片识别配置（开关 / 视觉模型 / 网关 / Key 密文）。

        读取失败按「关闭」处理：识别是增强能力，配置读不到时绝不能影响
        监听线程，退回原有的「图片消息 skipped」行为。
        """
        try:
            return vision.image_settings(self._db)
        except Exception:
            logger.exception("读取图片识别配置失败（按关闭处理）")
            return {"enabled": False, "model": "", "base_url": "",
                    "api_key_enc": "", "timeout_sec": 60}

    def _log_image_skipped(self, username: str, sender_wxid: str, mtype: str,
                           raw_content: str, reason: str,
                           local_id: int = 0) -> None:
        """图片消息跳过日志（input_text 存友好摘要，绝不存原始 XML）。

        ``raw_content`` 用入队时带过来的原文：worker 线程里原始消息 dict
        可能已被复用/回收，只带字符串最安全。

        ``error`` 末尾统一追加「（会话 local_id=xxx）」：用户看到「这张图没
        被识别」时，必须能自己定位到具体是哪张图（与「队列已满」那条口径
        一致）。``input_text`` 保持 ``[图片]`` 之类的展示文本，不塞 ID。
        """
        try:
            detail = f"{reason}（{username} local_id={local_id}）"
            self._db.insert_analysis_log(
                session_username=username,
                sender_wxid=sender_wxid,
                msg_count=1,
                input_text=content_format.format_content(
                    mtype, str(raw_content or ""))[:200],
                status="skipped",
                error=detail,
                # 带 local_id 落库：前端「重新识别」要按 (会话, local_id)
                # 精确定位这张图（日志正文里没有可用标识）
                image_local_id=local_id or None,
            )
        except Exception:
            logger.exception("写入图片跳过日志失败（已忽略）")

    @staticmethod
    def _vision_error_text(text: str) -> str:
        """把视觉模型的失败文案统一成「图片识别失败：…」前缀。

        与「图片非车辆/汽车配件（AI 识别：…）」这**正常结论**区分开：分析
        日志里一眼能看出是「模型没答应/额度/超时」还是「模型说不是配件」。
        """
        reason = str(text or "").strip() or "未知原因"
        for prefix in ("图片识别失败", "图片识别未配置", "图片预处理失败"):
            if reason.startswith(prefix):
                return reason
        return "图片识别失败：" + reason

    @staticmethod
    def _vin_reject_note(result: dict) -> str:
        """把「VIN 被丢弃」的原因转成可直接展示的中文说明。

        批次 9：视觉模型给过 VIN 但被拦下（图太小 / 校验位不符）时，用户会
        发现「这次没有车架号」——必须能解释为什么，否则会以为识别退化了。
        """
        reason = str(result.get("vin_rejected") or "")
        if not reason:
            return ""
        if reason == "low_res":
            return "未提取车架号：图片分辨率过低（微信只给了缩略图，字符无法辨认）"
        if reason == "check_digit":
            return "未提取车架号：读取结果校验位不符（疑似误读，已丢弃）"
        return "未提取车架号"

    def _save_image_analysis(self, username: str, local_id: int, *,
                             is_vehicle_part: bool, part_name: str = "",
                             vin: str = "", model: str = "",
                             error: str = "",
                             prompt_tokens: Optional[int] = None,
                             completion_tokens: Optional[int] = None) -> None:
        """落库图片识别结果（**只用于成功拿到模型 JSON 的结论**）。

        失败（额度/超时/Key/未配置/异常）一律**不落库**：缓存一旦写下
        ``is_vehicle_part=0``，额度恢复后这张图会被永久当成「非配件」不再
        识别（假阴性）。同一个 local_id 本来也不会重复投递，缓存的价值在
        「重投/重试」而不是「记录失败」。

        同时把本次视觉调用消耗的 token 记入 ai_usage（usage_kind=vision），
        账单分项「图片识别」的数据源；记录失败不影响识别主流程。
        """
        try:
            self._db.save_image_analysis(
                username, local_id, is_vehicle_part=is_vehicle_part,
                part_name=part_name, vin=vin, model=model, error=error)
        except Exception:
            logger.exception("保存图片识别结果失败（已忽略）")
        if prompt_tokens is not None or completion_tokens is not None:
            try:
                self._db.insert_ai_usage(
                    prompt_tokens=int(prompt_tokens or 0),
                    completion_tokens=int(completion_tokens or 0),
                    model=model, session_username=username,
                    usage_kind="vision")
            except Exception:
                logger.exception("记录图片识别 token 用量失败（已忽略）")

    # ------------------------------------------------------------------
    # 图片识别队列：监听线程侧（入队）与 worker 侧（取图/识别/聚合）
    # ------------------------------------------------------------------
    def _enqueue_image_message(self, msg: dict, *, username: str, kind: str,
                               sender_wxid: str, sender_name: str,
                               mtype: str) -> None:
        """监听线程侧：构造任务并入队，**不做任何识别**（必须极快返回）。

        开关关闭时保持历史行为：同步落一条 skipped 日志、文案逐字不变，
        既不占队列也不花 token。

        Args:
            msg: Listener 原始消息 dict（只读取 content/local_id/create_time）。
            username: 会话 username（群为 xxx@chatroom）。
            kind: group / friend。
            sender_wxid: 权威解析后的发送人 ID。
            sender_name: 发送人显示名。
            mtype: 原始消息类型（日志摘要用）。
        """
        settings = self._image_vision_settings()
        raw_content = str(msg.get("content") or "")
        try:
            local_id = int(msg.get("local_id") or 0)
        except (TypeError, ValueError):
            local_id = 0
        if not settings.get("enabled"):
            self._log_image_skipped(
                username, sender_wxid, mtype, raw_content,
                f"非文本消息（{mtype or '未知类型'}），不分析", local_id)
            return
        task = {
            "username": username,
            "local_id": local_id,
            "mtype": mtype,
            "kind": kind,
            # 会话显示名在监听线程里取（有缓存，足够快）；worker 里再取会多
            # 一次微信库调用，还可能撞上冷分片
            "session_name": self.display_name_with_corp(username),
            "sender_wxid": sender_wxid,
            "sender_name": sender_name,
            # 原始消息时间：合并窗口静默计时与「[HH:MM]」行时间都以它为准，
            # 换成识别完成时刻会让图片串到别的窗口里
            "create_time": msg.get("create_time"),
            "content": raw_content,
        }
        try:
            self._vision_queue.put_nowait(task)
        except queue.Full:
            # 背压：宁可少识别几张图，也不能阻塞监听线程或让队列无界增长
            self._log_image_skipped(username, sender_wxid, mtype,
                                    raw_content, "图片识别队列已满，跳过识别",
                                    local_id)
            logger.warning("图片识别队列已满（上限 %d），丢弃任务：%s "
                           "local_id=%s", VISION_QUEUE_MAX, username, local_id)
            return
        self._warn_vision_backlog(self._vision_queue.qsize())
        logger.info("图片任务已入队（%s local_id=%s，队列 %d/%d）",
                    username, local_id, self._vision_queue.qsize(),
                    VISION_QUEUE_MAX)

    def _warn_vision_backlog(self, size: int) -> None:
        """队列积压告警（限频 60 秒一次，避免逐条消息刷屏）。"""
        if size <= VISION_QUEUE_WARN:
            return
        now = time.monotonic()
        with self._vision_lock:
            if now - self._vision_warn_ts < 60:
                return
            self._vision_warn_ts = now
        logger.warning("图片识别队列积压 %d 条（上限 %d，worker %d 个），"
                       "识别结果可能明显延迟", size, VISION_QUEUE_MAX,
                       VISION_WORKERS)

    def _start_vision_workers(self) -> None:
        """启动图片识别 worker 线程（重复调用无副作用）。"""
        with self._vision_lock:
            alive = [t for t in self._vision_threads if t.is_alive()]
            self._vision_threads = alive
            started = 0
            for idx in range(len(alive), VISION_WORKERS):
                thread = threading.Thread(
                    target=self._vision_worker_loop, args=(idx,),
                    name=f"kefu-vision-{idx}", daemon=True)
                thread.start()
                self._vision_threads.append(thread)
                started += 1
        if started:
            logger.info("图片识别队列已启动（worker %d 个，队列上限 %d）",
                        VISION_WORKERS, VISION_QUEUE_MAX)

    def _stop_vision_workers(self) -> None:
        """停止 worker：先投哨兵再 join（每个最多等 5 秒）。

        在途的那一张图最长可能跑到视觉超时上限（60s），join 只等 5 秒就把
        剩余工作交给 daemon 线程随进程退出，避免拖慢停机流程。
        """
        with self._vision_lock:
            threads = list(self._vision_threads)
            self._vision_threads = []
        if not threads:
            return
        for _ in threads:
            try:
                self._vision_queue.put_nowait(None)     # None = 停止哨兵
            except queue.Full:
                logger.warning("图片识别队列已满，停止哨兵投递失败")
        for thread in threads:
            thread.join(timeout=5)
        remaining = self.vision_queue_size
        if remaining:
            logger.warning("图片识别队列停止时仍有 %d 条未处理", remaining)

    def _vision_worker_loop(self, index: int) -> None:
        """worker 主循环：取任务 → 处理 → 标记完成。

        约定：**任何异常都不得终止线程**（与监听/补转写线程一致），否则
        后续图片会静默堆积在队列里没人消费。
        """
        while True:
            task = self._vision_queue.get()
            if task is None:            # 停止哨兵
                self._vision_queue.task_done()
                logger.info("图片识别 worker %d 已退出", index)
                return
            ok = False
            try:
                ok = self._process_image_task(task)
            except Exception:           # 理论上 _process_image_task 已兜住
                logger.exception("图片识别 worker %d 处理任务异常（已忽略）",
                                 index)
            finally:
                self._count_vision(ok)
                self._vision_queue.task_done()

    def _count_vision(self, ok: bool) -> None:
        """累计识别任务计数（/api/status 可观测；失败不影响主流程）。"""
        with self._vision_lock:
            if ok:
                self._vision_ok += 1
            else:
                self._vision_fail += 1

    def _process_image_task(self, task: dict) -> bool:
        """处理一张待识别图片：取图 → 识别 → 落库 → （车辆/配件）进聚合。

        流程：
        1. 命中 ``image_analysis`` 缓存 → 直接用（同一张图绝不重复识别）；
        2. 取图（``get_image_bytes`` 已保证完整/不花屏）→ 调视觉模型
           （**调模型前复查图片识别开关**：任务排队期间用户可能关掉开关，
           此时落一条 skipped 直接返回，绝不产生 token）；
        3. 是车辆/配件 → 合成文本消息（``image_sell=True``）进聚合窗口；
           不是 → 落 skipped 带 AI 原因；
        4. **只有成功拿到模型 JSON 才写缓存**（无论结论是/不是配件）；
        5. 「原图还没下载完」这类**可恢复**失败（``get_image_bytes`` 返回
           None / ``vision.MSG_INCOMPLETE``）**不落库、也不丢**：排进
           ``ImageRetryQueue`` 按退避序列补识别（见 app/image_retry.py），
           否则这张图就永久漏识别；
        6. 其它失败（额度 / 超时 / Key / 未配置 / 异常）**一律不写缓存、
           不重试**，只落 skipped 日志：失败落库会把这张图永久钉成
           「非配件」，额度恢复后也不会再识别（假阴性）；而额度不足这类
           原因重试多少次都一样，反复重试只会白烧 token。

        Returns:
            True = 已有结论（含「非车辆/配件」这种正常结论）；
            False = 取图/识别失败（原因已落 skipped 日志）。
        """
        username = str(task.get("username") or "")
        mtype = str(task.get("mtype") or "")
        sender_wxid = str(task.get("sender_wxid") or "")
        raw_content = str(task.get("content") or "")
        try:
            local_id = int(task.get("local_id") or 0)
        except (TypeError, ValueError):
            local_id = 0
        # 与监听侧「收到图片消息/已入队」呼应：worker 开工时间点留痕。三段
        # 日志齐备 = 链路健康；缺任何一段即可定位卡点（1.2.7 openim 图片 40
        # 零痕迹事故的观测性兜底）。
        logger.info("开始处理图片识别任务（%s local_id=%s）", username, local_id)
        try:
            if local_id <= 0:
                self._log_image_skipped(username, sender_wxid, mtype,
                                        raw_content,
                                        "图片消息缺少 local_id，未识别", 0)
                return False
            cached = None
            try:
                cached = self._db.get_image_analysis(username, local_id)
            except Exception:
                logger.exception("读取图片识别缓存失败（按未命中处理）")
            if cached is not None:
                # 缓存里只可能是「成功拿到模型 JSON」的结论（失败不落库）
                if int(cached.get("is_vehicle_part") or 0):
                    self._emit_image_sell(task, cached)
                    return True
                reason = str(cached.get("error") or "").strip()
                if reason:
                    text = f"图片非车辆/汽车配件（AI 识别：{reason}）"
                else:
                    text = "图片已识别，非车辆/汽车配件"
                self._log_image_skipped(username, sender_wxid, mtype,
                                        raw_content, text, local_id)
                return True
            # 开关复查（批次 7 QA-P3-6）：开关原来只在监听侧
            # ``_enqueue_image_message`` 检查；任务已经在识别队列里时，用户
            # 关掉开关，worker 仍会调 1 次视觉模型（用户明确在意 token 成本）。
            # 这里每次任务现读一次配置，关闭则落一条 skipped 直接返回：
            # **不调模型、不入补识别队列、不落库**。
            if not self._image_vision_settings().get("enabled"):
                self._log_image_skipped(username, sender_wxid, mtype,
                                        raw_content,
                                        "图片识别开关已关闭，跳过识别",
                                        local_id)
                return False
            data, reason = self.get_image_bytes_with_reason(
                username, local_id)
            if not data:
                # 实测这是「图片还没下载好」的**主路径**：load_image 取不到
                # 完整图片时直接返回 None，根本走不到视觉调用，也就不会有
                # VisionError(MSG_INCOMPLETE)。所以这里同样要排队补识别，
                # 否则微信稍后才下载完的图永远漏识别。
                self._queue_image_retry(
                    task, username=username,
                    sender_wxid=sender_wxid, mtype=mtype,
                    raw_content=raw_content, local_id=local_id,
                    reason=f"图片读取失败或不完整（{reason or '原因未知'}），"
                           "未识别")
                return False
            settings = self._image_vision_settings()
            model = str(settings.get("model") or "")
            result = vision.analyze_image(
                data,
                base_url=str(settings.get("base_url") or ""),
                api_key=decrypt_api_key(settings.get("api_key_enc") or ""),
                model=model,
                timeout_sec=int(settings.get("timeout_sec") or 60))
            is_part = bool(result.get("is_vehicle_part"))
            reason_text = str(result.get("reason") or "").strip()
            if not is_part:
                # 成功拿到 JSON 且判定「不是」：结论落库（带一句话原因），
                # 下次命中缓存时能直接复用同一段原因，不必再问模型
                self._save_image_analysis(
                    username, local_id, is_vehicle_part=False,
                    model=model, error=reason_text,
                    prompt_tokens=result.get("prompt_tokens"),
                    completion_tokens=result.get("completion_tokens"))
                self._log_image_skipped(
                    username, sender_wxid, mtype, raw_content,
                    f"图片非车辆/汽车配件（AI 识别：{reason_text or '不确定'}）",
                    local_id)
                return True
            self._save_image_analysis(
                username, local_id, is_vehicle_part=True,
                part_name=str(result.get("part_name") or ""),
                vin=str(result.get("vin") or ""), model=model,
                error=self._vin_reject_note(result),
                prompt_tokens=result.get("prompt_tokens"),
                completion_tokens=result.get("completion_tokens"))
            self._emit_image_sell(task, result)
            return True
        except vision.VisionError as exc:
            text = str(exc)
            if text == vision.MSG_INCOMPLETE:
                # 微信可能还没下载完原图：不写缓存，排进补识别队列等原图落库
                self._queue_image_retry(task, username=username,
                                        sender_wxid=sender_wxid, mtype=mtype,
                                        raw_content=raw_content,
                                        local_id=local_id,
                                        reason=f"{text}，未识别")
            else:
                # 临时性失败（额度/限流/超时/Key/未配置）：**绝不落库**，
                # 否则额度恢复后这张图会被永久判成「非配件」不再识别
                self._log_image_skipped(
                    username, sender_wxid, mtype, raw_content,
                    self._vision_error_text(text), local_id)
            return False
        except Exception:
            logger.exception("图片识别任务异常（%s local_id=%s）",
                             username, local_id)
            self._log_image_skipped(username, sender_wxid, mtype,
                                    raw_content, "图片识别异常，未识别",
                                    local_id)
            return False

    def _queue_image_retry(self, task: dict, *, username: str,
                           sender_wxid: str, mtype: str, raw_content: str,
                           local_id: int, reason: str) -> bool:
        """把一张「原图还没下载完」的图片排进补识别队列，并落一条 skipped 日志。

        日志文案在原有前缀（``图片读取失败或不完整，未识别`` /
        ``图片不完整或已损坏，未识别``）后面追加「已排入重试队列」，让用户
        知道这张图**不会被永久丢掉**，不必自己手动处理。

        Args:
            task: ``_enqueue_image_message`` 构造的任务 dict（含 create_time /
                session_name，补识别成功后要用它进聚合窗口）。
            username: 会话 username。
            sender_wxid: 权威解析后的发送人 ID。
            mtype: 原始消息类型（日志摘要用）。
            raw_content: 原始消息原文（日志摘要用）。
            local_id: 图片消息的会话内 ID（去重键之一，日志里必须出现）。
            reason: 原始失败原因（保留旧前缀，避免老文案断链）。

        Returns:
            True = 本次新排入队列；False = 已在队列中（去重）或入队失败。
        """
        queued = False
        try:
            queued = bool(self._image_retry.enqueue(username, local_id, task))
        except Exception:
            logger.exception("图片补识别队列入队失败（已忽略）")
        if queued:
            text = f"{reason}，已排入重试队列（微信下载完原图后会自动补识别）"
        else:
            text = f"{reason}，该图已在补识别队列中，等待自动补识别"
        self._log_image_skipped(username, sender_wxid, mtype, raw_content,
                                text, local_id)
        return queued

    def _on_image_retry_dropped(self, username: str, local_id: int,
                                task: dict) -> None:
        """补识别队列满丢弃最旧任务时，落一条用户可见的 skipped 日志。

        队列内部的 ``logger.warning`` 用户看不到，页面上「这张图怎么没识别」
        无从解释；这里补一条与「图片识别队列已满，跳过识别」同口径的分析日志，
        文案里带上被丢弃的 local_id（由 ``_log_image_skipped`` 统一追加
        ``（{username} local_id={local_id}）``）。

        Args:
            username: 被丢弃任务所属会话。
            local_id: 被丢弃图片的会话内 ID（去重键之一）。
            task: 入队时带的原始任务 dict（取发送人/类型/原文写日志）。
        """
        payload = dict(task or {})
        try:
            sender_wxid = str(payload.get("sender_wxid") or "")
            mtype = str(payload.get("mtype") or "")
            raw_content = str(payload.get("content") or "")
            self._log_image_skipped(
                str(username or payload.get("username") or ""),
                sender_wxid, mtype, raw_content,
                "图片补识别队列已满，已丢弃最旧任务",
                local_id)
        except Exception:
            logger.exception("写图片补识别队列丢弃日志失败（已忽略）")

    def _on_image_retry_given_up(self, username: str, local_id: int,
                                 task: dict, attempts: int) -> None:
        """补识别把退避次数用尽时，落一条用户可见的 skipped 终态日志。

        此前用尽后只在服务日志里留一行 ``info``，用户在页面上看到的最后一
        条还是「第 4 次重试：图片仍未就绪（…），继续等待后自动补识别」，
        于是会一直等下去。实际上微信原图是**按需下载**的：群图长期只有半截
        缩略图（``_t.dat`` 尾部没有 JPEG 的 ``FF D9``），不去微信里点开就
        永远不会就绪，自动重试到天荒地老也没用。

        所以这里写一条明确的终态：告诉用户「不会自己好了」，并给出唯一有效
        的补救动作——在微信里点开这张图，再回来点「重新识别」（对应
        ``POST /api/images/retry``）。

        Args:
            username: 会话 username（群为 ``xxx@chatroom``）。
            local_id: 该图片消息在会话内的 local_id。
            task: 入队时的原始任务 dict（取发送人/类型/原文写日志）。
            attempts: 实际尝试次数（退避序列长度）。
        """
        payload = dict(task or {})
        try:
            sender_wxid = str(payload.get("sender_wxid") or "")
            mtype = str(payload.get("mtype") or "")
            raw_content = str(payload.get("content") or "")
            self._log_image_skipped(
                str(username or payload.get("username") or ""),
                sender_wxid, mtype, raw_content,
                f"已自动重试 {int(attempts)} 次仍未识别成功，已放弃自动补识别"
                "（微信原图按需下载，不会自己好：若上面日志写明「密钥未就绪」，"
                "请先在微信中打开任意一张图片并到设置页点「重新检查」捕获密钥；"
                "否则请在微信里点开该图片查看大图），之后可点「重新识别」补回",
                local_id)
        except Exception:
            logger.exception("写图片补识别最终失败日志失败（已忽略）")

    def _retry_image_task(self, task: dict) -> str:
        """补识别队列的 handler：再取一次图并识别。

        调用时机：worker 判定「原图还没下载完」后，由
        :class:`app.image_retry.ImageRetryQueue` 按退避序列（10/30/90/300s）
        回调，``task`` 里带 ``retry_attempt``（从 1 开始）。

        决策口径（对应「宁可多试几次，也别丢商机；但不反复烧 token」）：

        a. ``image_analysis`` 已有结论 → 直接复用（同一个 local_id 重试时可能
           已经在别的路径得出过结论），返回 ``"done"``，**不再调模型**；
        b. 取不到图 / 图不完整 → ``"retry"``（微信原图可能还要更久才落库）；
        c. 识别成功 → 落库 + ``_emit_image_sell``（按卖货进聚合窗口）；
        d. ``VisionError`` 且不是「图片不完整」（额度/限流/Key/未配置/预处理）
           → ``"abort"``：这类原因重试多少次都一样，反复调用只会白烧 token；
        e. 其它异常 → ``"abort"``（同上，且不把异常抛回调度线程）。

        Returns:
            ``"done"`` / ``"retry"`` / ``"abort"``（语义见 image_retry 模块）。
        """
        username = str(task.get("username") or "")
        mtype = str(task.get("mtype") or "")
        sender_wxid = str(task.get("sender_wxid") or "")
        raw_content = str(task.get("content") or "")
        try:
            attempt = int(task.get("retry_attempt") or 1)
        except (TypeError, ValueError):
            attempt = 1
        tag = f"第 {attempt} 次重试："
        try:
            local_id = int(task.get("local_id") or 0)
        except (TypeError, ValueError):
            local_id = 0
        if local_id <= 0:
            logger.warning("图片补识别任务缺少 local_id，放弃（%s）", username)
            return "abort"

        # 开关可能在排队期间被关掉（用户随时能在设置页关）：开关是「不许花
        # token」的总闸，这里每次尝试都现读一次，关闭即放弃，绝不再调模型。
        if not self._image_vision_settings().get("enabled"):
            self._log_image_skipped(
                username, sender_wxid, mtype, raw_content,
                f"{tag}图片识别开关已关闭，放弃补识别", local_id)
            return "abort"

        # a. 已有结论 → 直接复用，绝不重复烧 token
        cached = None
        try:
            cached = self._db.get_image_analysis(username, local_id)
        except Exception:
            logger.exception("读取图片识别缓存失败（按未命中处理）")
        if cached is not None:
            if int(cached.get("is_vehicle_part") or 0):
                self._emit_image_sell(task, cached)
                self._log_image_skipped(
                    username, sender_wxid, mtype, raw_content,
                    f"{tag}已有识别结论（车辆/汽车配件），已按卖货送入分析",
                    local_id)
            else:
                reason = str(cached.get("error") or "").strip()
                detail = (f"图片非车辆/汽车配件（AI 识别：{reason}）"
                          if reason else "图片已识别，非车辆/汽车配件")
                self._log_image_skipped(username, sender_wxid, mtype,
                                        raw_content, f"{tag}{detail}",
                                        local_id)
            return "done"

        # b. 取图：取不到 = 微信还没下载好这张图，继续等
        data, reason = self.get_image_bytes_with_reason(username, local_id)
        if not data:
            # 写清到底缺什么（原图没下载 / 缩略图不完整 / 解密失败）：
            # 只说「原图仍未就绪」会让用户误以为再等等就好，实际很多图
            # 微信永远不会自动下载原图（见 media.load_image 的候选顺序）
            self._log_image_skipped(
                username, sender_wxid, mtype, raw_content,
                f"{tag}图片仍未就绪（{reason or '原因未知'}），"
                "继续等待后自动补识别", local_id)
            return "retry"

        # c/d/e. 识别
        settings = self._image_vision_settings()
        model = str(settings.get("model") or "")
        try:
            result = vision.analyze_image(
                data,
                base_url=str(settings.get("base_url") or ""),
                api_key=decrypt_api_key(settings.get("api_key_enc") or ""),
                model=model,
                timeout_sec=int(settings.get("timeout_sec") or 60))
        except vision.VisionError as exc:
            text = str(exc)
            if text == vision.MSG_INCOMPLETE:
                # 图还是坏的（原图仍在下载中）：不写缓存，继续退避重试
                self._log_image_skipped(
                    username, sender_wxid, mtype, raw_content,
                    f"{tag}{text}，继续等待后自动补识别", local_id)
                return "retry"
            # 额度/限流/Key/未配置/预处理：重试无意义，立刻放弃
            self._log_image_skipped(
                username, sender_wxid, mtype, raw_content,
                self._vision_error_text(f"{tag}{text}"), local_id)
            return "abort"
        except Exception:
            logger.exception("图片补识别任务异常（%s local_id=%d 第 %d 次）",
                             username, local_id, attempt)
            self._log_image_skipped(username, sender_wxid, mtype, raw_content,
                                    f"{tag}图片识别异常，已放弃重试", local_id)
            return "abort"

        is_part = bool(result.get("is_vehicle_part"))
        reason_text = str(result.get("reason") or "").strip()
        if not is_part:
            self._save_image_analysis(
                username, local_id, is_vehicle_part=False,
                model=model, error=reason_text,
                prompt_tokens=result.get("prompt_tokens"),
                completion_tokens=result.get("completion_tokens"))
            self._log_image_skipped(
                username, sender_wxid, mtype, raw_content,
                f"{tag}图片非车辆/汽车配件（AI 识别：{reason_text or '不确定'}）",
                local_id)
            return "done"
        self._save_image_analysis(
            username, local_id, is_vehicle_part=True,
            part_name=str(result.get("part_name") or ""),
            vin=str(result.get("vin") or ""), model=model,
            error=self._vin_reject_note(result),
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("completion_tokens"))
        self._emit_image_sell(task, result)
        self._log_image_skipped(
            username, sender_wxid, mtype, raw_content,
            f"{tag}补识别成功，判定为车辆/汽车配件，已按卖货送入分析",
            local_id)
        return "done"

    def _emit_image_sell(self, task: dict, row: dict) -> None:
        """把「是车辆/配件」的图片合成一条文本消息送进聚合窗口。

        合成 content 是关键：后续预筛、AI 提示词、商机原文都基于它，AI 才能
        看见「[图片] 识别：车辆/汽车配件（发动机总成）车架号：…」这段线索；
        同时置 ``image_sell`` 让管线强制 direction=sell。

        ``create_time`` 用**原始消息时间**（入队时带的），不用识别完成时刻。
        """
        text = self._image_sell_text(row)
        username = str(task.get("username") or "")
        msg = {
            "username": username,
            "type": "文本",
            "content": text,
            "local_id": int(task.get("local_id") or 0),
            "sender_username": str(task.get("sender_wxid") or ""),
            "sender_display_name": str(task.get("sender_name") or ""),
            "create_time": task.get("create_time"),
            "image_sell": True,
        }
        self._aggregator.add(msg, str(task.get("session_name") or ""),
                             str(task.get("kind") or "friend"))
        logger.info("[图片识别] %s 的图片判定为车辆/配件（%s%s），已按卖货送入分析",
                    username, str(row.get("part_name") or "") or "未识别部件",
                    f"，车架号 {row.get('vin')}"
                    if str(row.get("vin") or "").strip() else "")

    @staticmethod
    def _image_sell_text(row: dict) -> str:
        """识别结论 → 送进分析的文本（部件名与车架号缺失时省略）。

        连续空白（含换行/全角空格）归一成单个半角空格：视觉模型偶发在
        部件名里返回换行（如「前保险杠\\n（左）」），而这条文本会整行进
        聚合窗口与商机原文——带换行会把一行拆成多行，商机详情按行匹配
        图片引用（raw_refs）时就永远对不上，图片无法显示（批次 11 M1）。
        """
        part = re.sub(r"\s+", " ", str(row.get("part_name") or "")).strip()
        vin = re.sub(r"\s+", " ", str(row.get("vin") or "")).strip()
        text = "[图片] 识别：车辆/汽车配件"
        if part:
            text += f"（{part}）"
        if vin:
            text += f" 车架号：{vin}"
        return text

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
        self._aggregator.add(msg, self.display_name_with_corp(username),
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
                self._aggregator.add(msg, self.display_name_with_corp(username),
                                     kind)
            except Exception:
                logger.exception("手动重试后重新纳入分析失败（已忽略）")
        return text, ""

    # ------------------------------------------------------------------
    # 图片手动补识别（分析日志「重新识别」）
    # ------------------------------------------------------------------
    def retry_image(self, username: str, local_id: int
                    ) -> tuple[bool, str]:
        """手动补识别一张图片：清掉旧结论后重新入图片识别队列。

        使用场景（用户实机反馈）：微信只在「用户点开那张图」或联网时后台
        自动下载原图，很多群图永远只有半个缩略图，自动补识别 4 次（约 7
        分钟）后放弃。用户看到日志后在微信里点开那张图，原图就落到本地了，
        这时点「重新识别」即可用原图重跑一次。

        Args:
            username: 会话 username。
            local_id: 图片消息在会话内的 local_id。

        Returns:
            ``(是否已受理, 用户可读中文提示)``。**绝不抛异常**。
        """
        username = str(username or "")
        try:
            lid = int(local_id)
        except (TypeError, ValueError):
            return False, "图片标识非法，无法重新识别"
        if not username or lid <= 0:
            return False, "缺少会话或图片标识，无法重新识别"
        try:
            if not self._image_vision_settings().get("enabled"):
                return False, "图片识别开关已关闭（设置 → AI 接口 → 图片识别）"
        except Exception:
            logger.exception("读取图片识别开关失败（按已关闭处理）")
            return False, "读取图片识别设置失败，请稍后重试"
        try:
            self._db.delete_image_analysis(username, lid)
        except Exception:
            logger.exception("清除图片识别缓存失败（继续按新图识别）")
        # 先探一次图：拿不到就直接告诉用户「微信还没下载原图」，别让他
        # 盯着一个注定失败的任务等 60 秒视觉超时
        data, reason = self.get_image_bytes_with_reason(username, lid)
        if not data:
            return False, (f"这张图仍未就绪：{reason or '原因未知'}。"
                           "请在微信里点开这张图（触发原图下载）后再试")
        task = {
            "username": username,
            "local_id": lid,
            "mtype": "图片",
            "kind": "group" if username.endswith("@chatroom") else "friend",
            "session_name": self.display_name_with_corp(username),
            "sender_wxid": "",
            "sender_name": "手动重新识别",
            "create_time": 0,
            "content": "[图片]",
        }
        try:
            self._vision_queue.put_nowait(task)
        except queue.Full:
            return False, "图片识别队列已满，请稍后再试"
        self._warn_vision_backlog(self._vision_queue.qsize())
        logger.info("图片手动补识别已受理（%s local_id=%d）", username, lid)
        return True, "已开始重新识别（约 10~20 秒），结果会出现在分析日志里"

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
        match = SENDER_ID_STRICT_RE.match(first_line)
        return match.group(1) if match else ""

    def _get_group_member_names(self, chatroom: str) -> dict[str, str]:
        """群成员 username → 显示名映射（缓存 TTL 600s）。

        wechatauto.get_group_members 返回字段：username / nick_name /
        remark / is_owner。显示名优先级（批次 5 需求 ②：与用户在微信里
        实际看见的名字一致）：

        1. ``group_cards`` 学习到的微信端显示名（session.db 的
           last_sender_display_name，即微信客户端渲染的群昵称/备注；
           微信 4.x 本地库不存群名片，contact.nick_name 是全局昵称，
           实测与群名片经常不同——这是此前显示错乱的根因）；
        2. ``remark``：我对该成员的好友备注；
        3. ``nick_name``：contact 表昵称（全局昵称）；
        4. 回退成员 username。

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
        # 叠加学习到的微信端显示名（群名片优先于全局昵称/备注：它就是
        # 微信 UI 渲染出来的名字；成员不在群成员表时忽略，防离群残留）
        if names:
            try:
                for uid, card in (self._db.get_group_cards(chatroom)
                                  or {}).items():
                    if uid in names and card:
                        names[uid] = card
            except Exception:
                logger.exception("读取群名片缓存失败（按无名片处理）")
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

        批次 6 需求 6：群聊里自己发送的消息在微信 4.x 本地库中
        ``real_sender_id = 2`` 且**正文没有发送者前缀**，因此走前缀解析
        会拿到空串 → 被判成「未知用户」并当成别人发的商机。这里把
        sender_id=2 提到最前面直接认定为自己（不依赖能否读到 self wxid，
        老库/转发场景仍保留原前缀+wxid 比对路径）。
        """
        if kind != "group":
            if msg.get("sender_id") in (2, "2"):
                return "", "我"
            return username, self.get_display_name(username)
        # 群聊里 sender_id=2 = 当前登录账号自己发送：没有前缀，直接定性
        if msg.get("sender_id") in (2, "2"):
            return self._self_id() or "", "我"
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
    # 过滤自己发送的消息（批次 5 需求 ⑤）
    # ------------------------------------------------------------------
    def _filter_self_enabled(self) -> bool:
        """设置「过滤自己发送的消息」是否开启（analysis.filter_self_messages，
        默认 "1"=开启）。设置读取失败按开启处理（与出厂默认一致）。"""
        try:
            return self._db.get_setting(KEY_ANALYSIS_FILTER_SELF, "1") != "0"
        except Exception:
            logger.exception("读取过滤自己消息设置失败（按开启处理）")
            return True

    # 与「自己」对话的会话：微信 4.x 的「给自己发消息」会话 username 就是
    # 自己的 wxid；文件传输助手同样是「自己发给自己」。这两类会话里每条
    # 消息的 sender_id 都是 2（自己），必须与「我在别人会话里发言」区分开。
    _SELF_CHAT_USERNAMES = ("filehelper",)

    def is_self_username(self, username: str) -> bool:
        """username 是否当前登录账号自己（微信「给自己发消息」的会话）。"""
        u = (username or "").strip()
        return bool(u) and u == self._self_id()

    def _is_self_chat(self, username: str) -> bool:
        """该会话是否「自己和自己对话」（给自己发消息 / 文件传输助手）。"""
        u = (username or "").strip()
        if not u:
            return False
        if u.lower() in self._SELF_CHAT_USERNAMES:
            return True
        return self.is_self_username(u)

    def _self_id(self) -> str:
        """当前登录账号 wxid（懒加载缓存；读取失败返回空串 = 不过滤群聊）。"""
        if self._self_wxid:
            return self._self_wxid
        getter = getattr(self._wxdb, "get_self_info", None) \
            if self._wxdb is not None else None
        if callable(getter):
            try:
                self._self_wxid = str((getter() or {}).get("username") or "")
            except Exception:
                logger.exception("读取当前登录账号失败（按空处理）")
        return self._self_wxid

    def _is_self_message(self, kind: str, msg: dict,
                         sender_wxid: str) -> bool:
        """该消息是否为当前登录账号自己发送。

        - 私聊：sender_id ∈ (2, "2") 即自己发送（微信原生标记）；
        - 群聊：sender_id ∈ (2, "2") 同样是自己发送（批次 6 需求 6：微信
          4.x 群聊里自己发的消息 ``real_sender_id = 2`` 且正文无前缀，
          老逻辑只比 wxid 判不出来，导致自己的消息被当商机分析）；其余
          情况解析出的发送人 wxid（正文前缀权威解析结果）等于当前登录
          账号 wxid，或消息自带的 sender_username 等于当前登录账号。
        """
        if kind != "group":
            return msg.get("sender_id") in (2, "2")
        # sender_id=2 是最可靠的「自己发送」标记，不依赖 _self_id() 是否读得到
        if msg.get("sender_id") in (2, "2"):
            return True
        self_id = self._self_id()
        if not self_id:
            return False
        return (sender_wxid == self_id
                or str(msg.get("sender_username") or "") == self_id)

    # ------------------------------------------------------------------
    # 群名片学习（批次 5 需求 ②：发送人显示名与微信一致）
    # ------------------------------------------------------------------
    def _learn_group_card(self, chatroom: str, sender_uid: str) -> None:
        """从 session.db 学习群成员的微信端显示名（群名片）并落库。

        微信 4.x 本地库不存群名片（``contact.nick_name`` 是全局昵称，
        实测成员「赵宇」在群里的名片是「513猎在于狩-宁静致远」），而
        SessionTable.last_sender_display_name 是微信客户端对最后一条消息
        发送者渲染的名称。群消息到达时若会话行的最后发送者就是本条消息
        的发送者，即把该显示名按 (群, 成员) 存入 group_cards 表，
        供 :meth:`_get_group_member_names` 优先采用。

        防张冠李戴：监听轮询间隙可能有他人消息先写进 session 表，
        ``last_msg_sender`` 与本条消息发送者不一致时不采纳。
        """
        wxdb = self._wxdb
        getter = getattr(wxdb, "get_session_row", None) \
            if wxdb is not None else None
        if not callable(getter):
            return
        try:
            row = getter(chatroom)
        except Exception:
            logger.exception("读取会话行失败（跳过群名片学习）：%s", chatroom)
            return
        if not row:
            return
        display = str(row.get("last_sender_display_name") or "").strip()
        if not display:
            return
        last_sender = str(row.get("last_msg_sender") or "")
        if last_sender and last_sender != sender_uid:
            return  # 会话行的最后发送者不是本条消息发送者：不采纳
        member = last_sender or sender_uid
        if not member or display == member:
            return
        try:
            self._db.save_group_card(chatroom, member, display)
        except Exception:
            logger.exception("保存群名片失败（已忽略）：%s/%s", chatroom, member)
            return
        # 群成员显示名缓存立即失效，下一次解析直接用上新名片
        with self._group_members_lock:
            self._group_members_cache.pop(chatroom, None)

    # ------------------------------------------------------------------
    # @人 ID 还原为显示名（批次 5 需求 ④：AI 上下文线索）
    # ------------------------------------------------------------------
    def resolve_at_tokens(self, chatroom: str, text: str) -> str:
        """把群聊文本里的 @人微信 ID token 还原为群内显示名。

        ``@wxid_xxx`` / ``@xxx@chatroom`` 等token 是 AI 判断「回复谁的
        报价/询价」的关键线索（批次 5 需求 ④）：裸删会丢上下文，这里
        用群成员映射把 ID 换成用户看得见的显示名（解析失败保留原 token，
        绝不篡改正文其它部分）。非群会话原样返回。
        """
        text = str(text or "")
        if not chatroom.endswith("@chatroom") or "@" not in text:
            return text
        names = self._get_group_member_names(chatroom)
        reverse = self._group_member_reverse_names(chatroom)

        def _sub(match: "re.Match[str]") -> str:
            token = match.group(0)[1:]          # 去掉前导 @
            # @xxx@chatroom / @xxx@openim 形态先剥后缀再查映射
            body = token
            for suffix in ("@chatroom", "@openim"):
                if body.endswith(suffix):
                    body = body[: -len(suffix)]
                    break
            display = (names.get(body)
                       or self.resolve_group_member_display(chatroom, body))
            if not display or display == body:
                # 显示名解析失败：尝试反查（token 可能是显示名本身）
                uid = reverse.get(body)
                if uid:
                    display = names.get(uid, body)
            return f"@{display}" if display and display != token \
                else match.group(0)

        return _AT_ID_TOKEN_RE.sub(_sub, text)

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
        session_display = self.display_name_with_corp(session_username) \
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

    def get_wechat_id(self, username: str) -> str:
        """username → 微信号（contact.alias），供界面替换原 wxid 展示。

        用户原话：「用户显示的微信ID，微信上看见的是用户的微信号，这块能不能
        显示为用户的微信号……统一显示我们能在微信中看见的微信号」。

        显示规则（与「显示名」严格区分）：
          1. ``contact.alias`` 非空 → 返回微信号（微信资料卡上可见的那一串）；
          2. alias 为空（部分账号从未设置微信号）→ 回退到备注/昵称
             （``get_display_name`` 已按 remark > nick_name 解析）；
             回退值等于 username 本身时视为「解析不到」，返回空串——wxid 是
             内部 ID，对用户无意义，不往「微信号」列里塞；
          3. username 为空 / 微信库未就绪 → 返回空串，由前端决定是否整列隐藏。

        结果与 :meth:`get_display_name` 共用各自的进程内缓存，单条查询只读
        一次 contact.db，适合商机/日志一页几十行逐行补全。

        Args:
            username: 联系人/会话 username。

        Returns:
            微信号（或备注/昵称回退值）；解析不到返回空串。
        """
        username = str(username or "")
        if not username:
            return ""
        # 群聊没有「微信号」这一标识（contact.alias 恒空），昵称回退会把群名
        # 塞进微信号位，语义不对且前端会和显示名重复；直接返回空串，让前端
        # 按各自规则回退到群名或群 ID。
        if username.endswith("@chatroom"):
            return ""
        with self._alias_cache_lock:
            hit = self._alias_cache.get(username)
        if hit is not None:
            return hit

        alias = ""
        if self._wxdb is not None:
            getter = getattr(self._wxdb, "get_alias", None)
            if callable(getter):
                try:
                    alias = str(getter(username) or "").strip()
                except Exception:
                    logger.debug("读取微信号(alias)失败（回退昵称）：%s",
                                 username, exc_info=True)
                    alias = ""
        if not alias:
            try:
                name = str(self.get_display_name(username) or "").strip()
            except Exception:
                name = ""
            # get_display_name 解析不到时会原样返回 username；不再回退 wxid
            alias = "" if not name or name == username else name
        with self._alias_cache_lock:
            self._alias_cache[username] = alias
        return alias

    # ------------------------------------------------------------------
    # 企业微信联系人的显示名（昵称@企业简称）
    # ------------------------------------------------------------------
    def openim_corp_map(self) -> dict:
        """企业微信联系人 → 企业简称（进程内缓存 600 秒）。

        底层 ``get_openim_corp_names`` 要读 contact.db（解密 + 查 openim_wording
        表），商机列表一页 20 行逐行调用会把接口拖垮，所以这里缓存整张表。
        读失败返回空字典并只缓存 30 秒，避免一次抖动把空表钉死 10 分钟。
        """
        now = time.monotonic()
        with self._corp_map_lock:
            ts, cached, ok = self._corp_map_cache
            if cached is not None and now - ts < (
                    CORP_MAP_CACHE_TTL_SEC if ok else 30.0):
                return cached
        if self._wxdb is None:
            return {}
        try:
            corp = self._wxdb.get_openim_corp_names() or {}
            ok = True
        except Exception as exc:
            logger.warning("读取企业微信企业简称失败（显示名退化为昵称）：%s", exc)
            corp, ok = {}, False
        with self._corp_map_lock:
            self._corp_map_cache = (now, corp, ok)
        return corp

    def display_name_with_corp(self, username: str) -> str:
        """username → 显示名；企业微信联系人拼成「昵称@企业简称」。

        为什么需要它（用户实机反馈）：微信 UI 里企业微信联系人就显示为
        「安佳胤@我的车车」，而本地库只能查到昵称「安佳胤」，于是商机来源
        列、发送人列看起来像「只有前半截」，同名好友与企业微信身份也分不清。
        非企业微信账号、取不到企业名时行为与 :meth:`get_display_name` 完全一致。
        """
        username = str(username or "")
        if not username:
            return ""
        name = self.get_display_name(username)
        if not self.is_openim(username):
            return name
        corp_map = self.openim_corp_map()
        return self._with_corp(name, str(corp_map.get(username) or ""))

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

    @staticmethod
    def _friend_row_ok(contact: dict) -> bool:
        """联系人行是否满足「可私信真好友」口径（对齐生产 SQL）。

        生产 ``get_contacts("friend")`` 已在 SQL 里过滤 delete_flag=0、
        local_type=1、verify_flag=0；这里对白名单行再补一道轻量复核，
        让替身/降级路径口径一致：即使某个替身未按 verify_flag 过滤，
        公众号（verify_flag≠0）也不会混进私聊候选。verify_flag 缺失或
        读不懂时保守放行（不误删已知好友）。

        Args:
            contact: get_contacts 返回的一条联系人 dict。

        Returns:
            True = 满足好友口径；False = 已删/公众号等，需排除。
        """
        if contact.get("delete_flag"):
            return False
        verify_flag = contact.get("verify_flag")
        if verify_flag is not None:
            try:
                if int(verify_flag) != 0:
                    return False
            except (TypeError, ValueError):
                pass
        return True

    @staticmethod
    def _alias_from_contact_row(row: dict, display_name: str,
                                username: str) -> str:
        """从已取回的 contact 行解析微信号（与 get_wechat_id 口径一致）。

        群聊没有微信号，直接返回空串；alias 非空时用它，否则回退显示名；
        显示名等于 username（解析不到）时也返回空串。

        Args:
            row: contact 行 dict（含 alias）。
            display_name: 已按 remark > nick_name > username 解析的显示名。
            username: 内部 ID。

        Returns:
            微信号字符串，解析不到返回空串。
        """
        if username.endswith("@chatroom"):
            return ""
        alias = str(row.get("alias") or "").strip()
        if alias:
            return alias
        return "" if not display_name or display_name == username \
            else display_name

    def _build_contact_list(self) -> list[dict]:
        """构建全量联系人/会话列表（群 + 好友 + 企业微信联系人，含显示名）。

        会话（≤100 条）的显示名/微信号从**已取回的整表联系人映射**一次性
        解析（原先逐条 get_display_name + get_wechat_id 反复开关 contact.db，
        100 会话最多 200 次连接，是首屏慢的主因）；contact 表无对应行的
        会话才回退逐条查询。公众号（gh_ 前缀）不参与监控，直接跳过。

        好友与群聊均取**全量**（``limit=0``）：本机好友可达 3 万条，
        原先固定 ``limit=2000`` 会把按 username 排序后的绝大多数好友
        截断掉，表现为「明明有这个人却搜不到」。

        私聊候选严格限定为**真实好友**：``get_sessions`` 里出现的非好友
        临时会话（群成员、已删好友）原先一律被标成 kind="friend"，搜索
        时会混进大量非好友。判定复用 ``get_contacts("friend"/"openim")``
        的 SQL 口径（**local_type=1 且 verify_flag=0 才是通讯录真好友**；
        3 是群内非好友、已排除；lt=1 里 verify_flag≠0 的是公众号——
        i黑马 wxid_2965349653612 / verify_flag=24 / 无备注 / extra_buffer
        有公众号实锤；5/6/7 企业微信；delete_flag=0，与 ``WeChatDB.is_friend``
        一致），整表取回后按集合比对，避免逐条 is_friend 反复打开
        contact.db（好友可达 3 万条）。实锤：搜「淼淼」命中的 5 个
        local_type=3 群成员（无微信号/无备注）不再混入，只剩 2 位真好友；
        搜「黑马」不再返回公众号 i黑马。给自己发消息的会话（is_self）
        按既有语义保留；群聊不受影响。

        企业微信联系人单独取 ``kind="openim"`` 并归入 friend（一对一
        私聊），带 ``openim=True`` 标记供前端区分同名联系人；能解析出
        所属企业时显示名按微信端习惯拼成「昵称@企业简称」。
        """
        corp_map = self._openim_corp_map()
        # 当前登录账号 wxid：微信 4.x 的「给自己发消息」是一个**普通会话**，
        # username 就是自己的 wxid（实测 wxid_xxx 会话里有自己发的消息），
        # 需要标记出来，否则在候选列表里和同名好友长得一模一样。
        self_id = self._self_id()
        # 先取全量联系人：保持 get_contacts 的调用顺序（chatroom/friend/
        # openim）与 limit=0 不变，同时用 friend+openim 构造好友白名单。
        contacts_by_kind: dict[str, list[dict]] = {}
        friend_ok = True
        openim_ok = True
        for c_kind in ("chatroom", "friend", "openim"):
            try:
                contacts_by_kind[c_kind] = self._wxdb.get_contacts(
                    kind=c_kind, limit=0)
            except Exception as exc:
                logger.warning("读取联系人（%s）失败：%s", c_kind, exc)
                contacts_by_kind[c_kind] = []
                if c_kind == "friend":
                    friend_ok = False
                elif c_kind == "openim":
                    openim_ok = False
        friend_usernames: set[str] = {
            str(c.get("username") or "")
            for c in contacts_by_kind.get("friend", [])
            if c.get("username") and self._friend_row_ok(c)
        }
        # openim 一路按 username 后缀（@openim/@kefu）选取，不检查 local_type；
        # 有 is_friend 能力时逐条复核（企业微信好友数量远小于普通好友，成本
        # 可控），口径与 WeChatDB.is_friend 的 local_type 5/6/7 + delete_flag
        # 严格一致，挡掉「@openim 但 local_type=0」的脏数据。
        # 替身/降级无该方法时沿用 get_contacts 结果，不误伤既有契约。
        is_friend_check = getattr(self._wxdb, "is_friend", None)
        for c in contacts_by_kind.get("openim", []):
            username = str(c.get("username") or "")
            if not username:
                continue
            if callable(is_friend_check):
                try:
                    if not is_friend_check(username):
                        continue
                except Exception:
                    logger.debug("复核企业微信好友关系失败（按保留处理）：%s",
                                 username, exc_info=True)
            friend_usernames.add(username)
        # 好友表读不到时放行会话里的 friend 候选（降级不误伤）；读到了才严格过滤。
        strict_friend_filter = friend_ok and openim_ok
        # 一次性把三路 contact 结果建成 username→行 映射：近期会话的显示名
        # /微信号直接从中解析，避免逐个 get_display_name + get_wechat_id
        # 反复开关 contact.db（100 个会话最多 200 次连接，首屏主要耗时点）。
        # 全量列表本来就已取回，此映射不增加任何 DB 查询，也不改变结果。
        contact_rows: dict[str, dict] = {}
        for rows in contacts_by_kind.values():
            for row in rows:
                row_user = str(row.get("username") or "")
                if row_user and row_user not in contact_rows:
                    contact_rows[row_user] = row
        items: list[dict] = []
        seen: set[str] = set()
        for s in self._wxdb.get_sessions(limit=100):
            username = str(s.get("username") or "")
            if not username or username in seen or username.startswith("gh_") \
                    or _is_system_account(username):
                continue
            is_group = username.endswith("@chatroom")
            is_self = bool(self_id) and username == self_id
            # 群聊候选原样保留；私聊候选只保留真实好友（含企业微信好友），
            # 非好友的临时会话/已删好友不再混入。self 会话按既有语义保留。
            if not is_group and strict_friend_filter and not is_self \
                    and username not in friend_usernames:
                continue
            openim = self.is_openim(username)
            corp = corp_map.get(username, "")
            row = contact_rows.get(username)
            if row is not None:
                # 与 get_nickname 同口径：remark > nick_name > username，
                # 直接复用已取回的整表行，不再逐条打开 contact.db。
                raw_name = (str(row.get("remark") or "")
                            or str(row.get("nick_name") or "") or username)
                alias = self._alias_from_contact_row(row, raw_name, username)
            else:
                # contact 表里没有对应行（给自己发消息、降级放行、替身缺行）
                # 才回退逐条查询；正常路径不再产生逐条 DB 连接。
                raw_name = self.get_display_name(username)
                alias = self.get_wechat_id(username)
            items.append({"username": username,
                          "display_name": self._with_corp(raw_name, corp),
                          # 微信号（alias）供界面替换 wxid 显示；已随整表
                          # 行一并拿到，零额外查询。
                          "alias": alias,
                          "kind": "group" if is_group else "friend",
                          "openim": openim, "corp": corp,
                          "is_self": is_self})
            seen.add(username)
        # wechatauto.get_contacts 的群聊取值为 "chatroom"（不是 "group"）；
        # openim 为企业微信/客服联系人，按一对一私聊归入 friend
        for c_kind, item_kind in (("chatroom", "group"), ("friend", "friend"),
                                  ("openim", "friend")):
            for c in contacts_by_kind.get(c_kind, []):
                username = str(c.get("username") or "")
                if not username or username in seen or username.startswith("gh_") \
                        or _is_system_account(username):
                    continue
                # 私聊候选（friend/openim）同样受好友白名单约束；openim
                # 白名单已按 is_friend 复核过 local_type/delete_flag。
                if c_kind in ("friend", "openim") and strict_friend_filter \
                        and username not in friend_usernames:
                    continue
                remark = str(c.get("remark") or "").strip()
                nick = str(c.get("nick_name") or "").strip()
                corp = corp_map.get(username, "")
                # 用户手动备注优先（微信里备注会盖掉「昵称@企业」）；
                # 无备注且能拿到企业简称时才拼后缀
                name = remark or self._with_corp(nick or username, corp)
                # alias 随 get_contacts 一同返回；为空时回退显示名也用已取回
                # 的行计算（与 get_wechat_id 口径一致）。3 万好友里约 1/4
                # 无 alias，原先逐个 get_wechat_id 会再开上千次 contact.db。
                raw_name = (str(c.get("remark") or "")
                            or str(c.get("nick_name") or "") or username)
                alias = self._alias_from_contact_row(c, raw_name, username)
                items.append({"username": username,
                              "display_name": name, "kind": item_kind,
                              "alias": alias,
                              "openim": c_kind == "openim"
                                        or self.is_openim(username),
                              "corp": corp,
                              "is_self": bool(self_id)
                                         and username == self_id})
                seen.add(username)
        logger.info("联系人列表构建完成：%d 条（群/好友/企业微信合并去重后，"
                    "其中 %d 个企业微信联系人有企业名）",
                    len(items), len(corp_map))
        return items

    def _openim_corp_map(self) -> dict[str, str]:
        """企业微信联系人 → 企业简称（经整表缓存的 :meth:`openim_corp_map`）。"""
        return self.openim_corp_map()

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

        排序（群聊/私聊一致，用户 2026-11 需求）：
          - 无关键词（首屏）：「最近有消息优先、last_timestamp 降序」，
            无消息的排最后（组内按原缓存顺序稳定）；
          - 有关键词：匹配质量 ``_match_rank`` 主排序，同组内时间降序，
            保证搜「淼淼」精确匹配排前。
        两种情况都先对**全量候选**排序再截 ``limit``，不能边收集边截断。

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
            # 首屏（无关键词）：按「有消息优先 + last_timestamp 降序」排列，
            # 群聊/私聊口径一致。关键正确性：先对**全量**候选排序，再截
            # limit（前端传 50）——若边收集边截断，全量里时间最靠前的会话
            # 可能落在被丢弃的尾部，用户就看不到最近联系人。
            # 无消息的排后面（has_msg=1），组内按原缓存顺序稳定（order）。
            # 3 万条内存排序仅几十毫秒，不是瓶颈（实测 SQL/建表才是）。
            times = self._get_session_time_map()
            ranked: list[tuple[int, int, int, dict]] = []
            for order, item in enumerate(items):
                if kind != "all" and item["kind"] != kind:
                    continue
                try:
                    ts = int(times.get(item["username"], 0) or 0)
                except (TypeError, ValueError):
                    ts = 0
                ranked.append((0 if ts else 1, -ts, order, item))
            ranked.sort(key=lambda t: (t[0], t[1], t[2]))
            return [dict(t[3]) for t in ranked[:limit]]
        # 有关键词：先收集全部命中，按匹配质量排序后再按 limit 截断。
        # 好友基数很大（本机约 3 万条），若边收集边截断，会出现
        # 「名字完全一致的联系人」被一堆昵称含同字的账号挤出前 50 条。
        # 同一匹配质量内按「最后消息时间倒序」排序（批次 5 需求 ③）：
        # 近期有过消息的会话排前面，无消息的最后（保持 _match_rank 主排序不变）。
        times = self._get_session_time_map()
        hits: list[tuple[int, int, int, dict]] = []
        for order, item in enumerate(items):
            if kind != "all" and item["kind"] != kind:
                continue
            name = item["display_name"]
            alias = str(item.get("alias") or "")
            # 微信号（alias）也纳入搜索：用户在微信里记住的常是微信号而非昵称。
            if kw not in name and kw not in alias \
                    and kw not in item["username"]:
                continue
            ts = times.get(item["username"], 0)
            hits.append((self._match_rank(name, item["username"], kw, alias),
                         0 if ts else 1, -ts, order, item))
        hits.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
        return [dict(t[4]) for t in hits[:limit]]

    def _get_session_time_map(self) -> dict[str, int]:
        """会话最后消息时间映射（username → last_timestamp，TTL 与联系人缓存一致）。

        数据源：wechatauto.get_session_times（session.db 全量会话行）；
        读取失败或替身未实现该方法时返回空映射（调用方按「无消息」处理）。
        """
        now = time.monotonic()
        if self._session_times and now - self._session_times_ts \
                < SESSION_CACHE_TTL_SEC:
            return self._session_times
        getter = getattr(self._wxdb, "get_session_times", None) \
            if self._wxdb is not None else None
        if callable(getter):
            try:
                self._session_times = dict(getter() or {})
                self._session_times_ts = now
            except Exception as exc:
                logger.warning("读取会话最后消息时间失败（按无时间处理）：%s", exc)
        return self._session_times

    @staticmethod
    def _match_rank(name: str, username: str, kw: str,
                    alias: str = "") -> int:
        """搜索匹配质量排序权重：越小越靠前。

        0 = 显示名/微信号完全等于关键词（用户最可能想找的人）
        1 = 显示名/微信号以关键词开头
        2 = 显示名/微信号包含关键词
        3 = 仅 username 命中（wxid / openim id 等内部 ID）
        4 = 未命中（调用方一般已过滤）

        企业微信联系人的显示名是「昵称@企业简称」，匹配时把 ``@`` 之后的
        企业名视作后缀：搜「邢不行」应命中原名并获得与普通好友相同的权重。

        Args:
            name: 显示名。
            username: 内部 ID（wxid / openim id）。
            kw: 搜索关键词。
            alias: 微信号（可选；缺省时保持旧调用方行为不变）。
        """
        base = name.split("@", 1)[0] if "@" in name else name
        wxid = str(alias or "").strip()
        if name == kw or base == kw or (wxid and wxid.lower() == kw.lower()):
            return 0
        if name.startswith(kw) or base.startswith(kw) \
                or (wxid and wxid.lower().startswith(kw.lower())):
            return 1
        if kw in name or (wxid and kw.lower() in wxid.lower()):
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

    def get_messages_in_range(self, username: str, start_epoch: int,
                              end_epoch: int, limit: int = 5000
                              ) -> tuple[list[dict], str]:
        """按时间范围读取会话消息（群消息总结的数据源）。

        微信库 create_time 是 epoch 秒，SQL 内只做整数比较；逐行复用现有
        取名链（正文前缀权威解析 + group_cards 学习缓存）、内容友好化与
        语音转写缓存回填，保证与消息列表/上下文展示口径一致。

        Args:
            username: 会话 username（群为 xxx@chatroom）。
            start_epoch: 起始 epoch 秒（含）。
            end_epoch: 结束 epoch 秒（含）。
            limit: 条数上限（5000，仅作病态输入兜底）。

        Returns:
            (msgs, error)：error 为空串表示成功；微信库不可用时返回
            ( [], MSG_DB_UNAVAILABLE_ERROR )。查询异常返回中文原因。
        """
        if self._wxdb is None:
            return [], MSG_DB_UNAVAILABLE_ERROR
        try:
            raw = self._wxdb.get_messages_between(
                username, int(start_epoch), int(end_epoch), limit=limit)
        except Exception as exc:
            logger.warning("按时间范围读取会话消息失败（%s）：%s", username, exc)
            return [], "微信数据库正忙或不可读，请稍后重试"
        out: list[dict] = []
        for row in raw or []:
            mtype = str(row.get("type") or "")
            content = str(row.get("content") or "")
            sender_id = int(row.get("sender_id") or 0)
            fallback_wxid = str(row.get("sender_username") or "")
            display, clean_content = self._resolve_row_sender(
                username, content, fallback_wxid, sender_id)
            # 群消息正文前缀是权威发送者：能把显示名反解成 wxid 时优先用它，
            # 否则沿用微信库 sender_id 映射出的 wxid（与消息列表口径一致）。
            sender_wxid = fallback_wxid
            if username.endswith("@chatroom") and sender_id not in (2, "2"):
                candidate = self.extract_content_sender(content)
                if candidate:
                    reverse = self._group_member_reverse_names(username)
                    sender_wxid = reverse.get(candidate, candidate)
            out.append({
                "local_id": int(row.get("local_id") or 0),
                "type": mtype,
                "type_name": mtype,
                "sender_id": sender_id,
                "sender_wxid": sender_wxid,
                "sender_name": display,
                "create_time": epoch_to_iso(row.get("create_time")),
                "content": content_format.format_content(mtype, clean_content),
                "sort_seq": int(row.get("sort_seq") or 0),
            })
        self._fill_voice_texts(username, out)
        return out, ""

    def _resolve_row_sender(self, username: str, content: str,
                            fallback_wxid: str,
                            sender_id: int = 0) -> tuple[str, str]:
        """历史消息的 (发送人显示名, 剥离前缀后的正文)。

        群聊与实时监听同一权威口径：正文首部「发送者ID:\\n」前缀优先
        （SenderName2Id 映射不可靠，反馈 6 群昵称错乱）。只有前缀能对上
        群成员（wxid 或群显示名）时才剥离，避免误伤「9:30 开会」这类正文；
        私聊发送人即会话对方，sender_id=2 为自己；企业微信/客服私聊按会话
        username 补上企业简称（行内 sender_username 是内部工作号，不可用作
        展示名，详见下方分支注释）。

        批次 6 需求 6：群聊里自己发送的消息 ``sender_id = 2`` 且正文**没有**
        前缀，此前会落到 ``candidate or "未知用户"`` 分支显示「未知用户」。
        这里最前面直接返回「我」，且不剥前缀（自己发的本来就没有前缀）。
        """
        text = str(content or "")
        if not username.endswith("@chatroom"):
            if sender_id in (2, "2"):
                return "我", text
            if self.is_openim(username):
                # 企业微信/客服私聊（反馈：上下文里冒出「杜飞」）：
                # 微信库消息行的 real_sender_id 解析出来的是**内部工作号**，
                # 并不是会话对端的 openim username。实测会话
                # 25984982152595070@openim（安佳胤@我的车车）里，对方消息
                # sender_username=25984983893057228@openim（昵称「杜飞」）。
                # 微信 UI、商机列表与详情头部都按「会话对端」显示，这里必须
                # 同口径：用会话 username 解析「昵称@企业简称」，不能用行内
                # 的 fallback_wxid。普通好友私聊仍按原逻辑取行内发送人。
                name = self.display_name_with_corp(username)
            else:
                name = self.get_display_name(fallback_wxid) if fallback_wxid else ""
            return name or "我", text
        # 群聊里 sender_id=2 = 自己发送：无前缀，显示「我」
        if sender_id in (2, "2"):
            return "我", text
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
            clean = SENDER_ID_STRICT_RE.sub("", text, count=1).lstrip("\n")
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

    def is_friend(self, username: str) -> bool:
        """该账号是否可私信的好友（群商机「私信回复」按钮是否可用）。

        口径完全委托 ``WeChatDB.is_friend``（local_type=1 且 verify_flag=0
        通讯录真好友 + 5/6/7 企业微信；local_type=3 群内非好友、
        verify_flag≠0 的公众号如 i黑马 均不可私信）。原先把 local_type=3
        当好友、把 local_type=1 里的公众号也当好友，导致群商机里给群内
        陌生人/公众号开的「私信回复」按钮点了必失败；修正后该按钮对
        lt=3 与公众号自动置灰，符合微信真实语义。

        微信库未就绪（降级）或读失败时一律返回 False：宁可把私信按钮置灰
        让人走群内回复，也不要在界面上承诺一个点了会失败的按钮。
        """
        if self._wxdb is None:
            return False
        try:
            if self.is_self_username(username):
                return False
            return bool(self._wxdb.is_friend(username))
        except Exception as exc:
            logger.debug("判断好友关系失败（%s）：%s", username, exc)
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
    def _on_image_key_ready(self, aes_key: str) -> None:
        """图片 AES 密钥可用：立即唤醒待补识别队列。

        1.2.4 现场是 22:29 取图失败入队，22:33 用户打开图片后密钥才落盘。
        旧实现只在「下一条新消息」到来时才再次取图，队列里的图片仍按
        10/30/90/300s 慢慢等，甚至用尽重试。这里把队列整体提前到期，
        让已缓存失败的图片马上重跑；失败仍走原有退避/放弃逻辑。
        """
        try:
            woken = self._image_retry.wake_all()
        except Exception:
            logger.exception("唤醒图片补识别队列失败（已忽略）")
            return
        if woken:
            logger.info("图片 AES 密钥已就绪，立即重试 %d 条待补识别图片", woken)

    def _get_media_downloader(self):
        """懒加载并复用 MediaDownloader（图片解密 / 语音下载共用）。

        首次调用会解密媒体库（本机约 13s），因此**不能**每条消息都新建；
        失败返回 None（由调用方转成用户可读原因）。
        """
        # 降级/选错账号时没有微信库：绝不能构造 MediaDownloader，否则状态
        # 轮询会反复抛 AttributeError（db.workdir）刷满日志。
        if self._wxdb is None:
            return None
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
                    self._wxdb, save_dir=str(IMG_CACHE_DIR),
                    key_ready_callback=self._on_image_key_ready)
            except Exception:
                logger.exception("创建 MediaDownloader 失败")
                return None
            return self._media_downloader

    def image_key_status(self) -> dict:
        """图片解密 AES 密钥是否就绪（只读查询，供设置页提醒使用）。

        检查顺序与解密时一致，但**绝不**调用 ``_scan_aes_key(monitor=True)``：
        内存扫描会等用户去微信点开图片，最长阻塞 120 秒，不能放在 HTTP 请求里。
        这里只看已缓存密钥、显式注入、cfgDword 派生与 image_keys.json 持久化
        缓存（后三者均用真实密文首块反测，与解密路径同一套校验）。

        Returns:
            ``{"ready": bool, "source": str, "recheck_running": bool,
            "recheck_last_ok": bool|None, "recheck_last_at": str}``；
            任一异常降级为未就绪。
        """
        result: dict = {"ready": False, "source": ""}
        try:
            downloader = self._get_media_downloader()
            if downloader is None:
                return result
            # 1) 本次进程内已捕获（用户打开图片后由监听线程写入）
            pair = getattr(downloader, "_img_key", None)
            if isinstance(pair, (tuple, list)) and len(pair) >= 1 and pair[0]:
                result.update(ready=True, source="memory")
                return result
            # 2) 显式注入密钥（如手工指定）
            injected = getattr(downloader, "_image_key", None)
            if injected and downloader._validate_key(injected):
                result.update(ready=True, source="injected")
                return result
            # 3) cfgDword 派生（确定性离线，无需看图）
            derived = downloader._derive_cfg_key()
            if derived and derived[0]:
                result.update(ready=True, source="derived")
                return result
            # 4) 本地 image_keys.json 持久化缓存（按账号键，经密文反测）
            cached = downloader._load_persisted_key()
            if cached:
                result.update(ready=True, source="cache")
                return result
        except Exception:
            logger.exception("读取图片密钥状态失败（按未就绪处理）")
        finally:
            # 主动捕获状态（设置页「重新检查」回显）；任意分支都要带上，
            # 否则前端无法区分「扫描进行中」和「从未扫描」。
            try:
                with self._image_key_recheck_lock:
                    recheck = dict(self._image_key_recheck)
            except Exception:
                recheck = {}
            result["recheck_running"] = bool(recheck.get("running"))
            result["recheck_last_ok"] = recheck.get("last_ok")
            result["recheck_last_at"] = str(recheck.get("last_at") or "")
            result["recheck_last_error"] = str(recheck.get("last_error") or "")
        return result

    def recheck_image_key(self, timeout_sec: int = 240) -> dict:
        """设置页「重新检查」：后台线程主动做一次有预算的密钥捕获。

        1.2.7 之前的「重新检查」只读状态、不触发任何捕获，用户点了毫无反应。
        现在改为：防重入地拉起后台线程，跑 :meth:`MediaDownloader.
        detect_image_key(refresh=True)`（只读内存扫描：OpenProcess +
        ReadProcessMemory，不写内存、不注入），捕获成功会自动持久化
        image_keys.json 并经就绪回调唤醒图片补识别队列；失败也会落一条
        app 日志并把原因写进状态（设置页可见）。

        Args:
            timeout_sec: 扫描墙钟预算（秒）。实测一轮全量扫描约 110s，
                默认 240s 保证首轮扫描不被截断，且给用户留出「点开图片」
                的时间窗。

        Returns:
            ``{"started": bool, "running": bool, "last_ok": ...}``：
            ``started=False`` 表示已有扫描在跑（重入被拒）。
        """
        with self._image_key_recheck_lock:
            if self._image_key_recheck.get("running"):
                return {"started": False, **self._image_key_recheck}
            self._image_key_recheck["running"] = True
            self._image_key_recheck["last_error"] = ""
        thread = threading.Thread(target=self._recheck_image_key_worker,
                                  args=(int(timeout_sec),),
                                  name="kefu-image-key-recheck", daemon=True)
        thread.start()
        logger.info("图片密钥主动捕获已启动（后台只读内存扫描，预算 %d 秒；"
                    "结果见设置页或稍后日志）", int(timeout_sec))
        return {"started": True, **self._image_key_recheck}

    def _recheck_image_key_worker(self, timeout_sec: int) -> None:
        """主动捕获的后台执行体：结果与异常都收敛到 ``_image_key_recheck``。"""
        ok = False
        err = ""
        try:
            downloader = self._get_media_downloader()
            if downloader is None:
                err = "媒体下载器不可用（微信库未就绪，无法扫描）"
            else:
                pair = downloader.detect_image_key(
                    refresh=True, monitor_timeout=max(5.0, float(timeout_sec)))
                if pair and pair[0]:
                    ok = True
                    logger.info("图片密钥主动捕获成功（来源：微信进程内存扫描，"
                                "已持久化；补识别队列已自动唤醒）")
                else:
                    err = ("本次扫描未捕获到密钥：请确认微信已登录，且在微信中"
                           "打开（查看大图）任意一张图片后重新检查")
                    logger.warning("图片密钥主动捕获未命中：%s", err)
        except Exception as exc:                    # 后台线程绝不能带异常退出
            logger.exception("图片密钥主动捕获异常")
            err = f"主动捕获异常：{exc}"
        finally:
            with self._image_key_recheck_lock:
                self._image_key_recheck.update({
                    "running": False,
                    "last_ok": ok,
                    "last_at": dbmod.now_iso(),
                    "last_error": err,
                })

    def get_image_bytes(self, username: str, local_id: int) -> Optional[bytes]:
        """解密会话中指定图片消息，返回图片字节；失败返回 None。

        与 :meth:`get_image_bytes_with_reason` 同一条链路，只是丢掉原因。
        """
        data, _reason = self.get_image_bytes_with_reason(username, local_id)
        return data

    def get_image_bytes_with_reason(self, username: str,
                                    local_id: int
                                    ) -> tuple[Optional[bytes], str]:
        """取图片字节，并返回**可直接展示给用户**的失败原因。

        底层用 wechatauto.media.MediaDownloader 定位并解密 .dat
        （群聊原图未点开时只有缩略图缓存，自动回退缩略图）。解密结果
        由 MediaDownloader 落盘到 IMG_CACHE_DIR（持久缓存，避免重复
        解密；目录在 data/ 下已被 git 忽略）。

        批次 6 待办 1：微信的 .dat 经常只下载了一部分，解出来是「顶部一条
        真图 + 下面整片纯绿」的花图，且旧版本会把花图写进缓存让人永远看到
        花图。现在整套链路只返回**内容合格**的图片（完整性 + 花屏双重校验，
        见 ``wechatauto.media.load_image``）：原图不合格回退缩略图，两者都
        不合格返回 None，由前端展示占位卡片——宁可占位也不要花图。

        仅接受可内嵌展示的格式（jpg/png/gif）；失败原因写入 info 日志
        （中文），便于用户在报障时直接看到「为什么这张图是空白」。
        """
        if self._wxdb is None:
            logger.info("图片不可用（%s local_id=%s）：微信库未就绪",
                        username, local_id)
            return None, "微信库未就绪"
        downloader = self._get_media_downloader()
        if downloader is None:
            logger.info("图片不可用（%s local_id=%s）：媒体下载器不可用",
                        username, local_id)
            return None, "媒体下载器不可用"
        try:
            data, _path, reason = downloader.load_image(username, local_id)
        except Exception as exc:
            logger.warning("图片解密失败（%s local_id=%s）：%s",
                           username, local_id, exc)
            return None, f"图片解密失败：{exc}"
        if not data:
            reason = reason or "未知原因"
            # 取图失败的底层文案通常是「原图与缩略图均不完整」，它只描述了
            # 现象。实测用户删掉 image_keys.json 后，真实原因是图片 AES 密钥
            # 缺失（cfgDword 派生/持久化都不可用），但分析日志里完全看不出来，
            # 用户既不知道去微信点开图片，也会误以为「图还没下载完」而一直等。
            # 这里补一次只读密钥探测（不会触发 120s 内存扫描），密钥确实未就绪
            # 时把原因换成可操作的提示；探测失败则保留原文案，绝不影响主流程。
            try:
                status = self.image_key_status()
                if not status.get("ready"):
                    reason = ("图片解密密钥未就绪，请在微信中打开任意一张图片"
                              "查看大图，程序捕获密钥后会自动补识别")
            except Exception:
                logger.debug("取图失败原因补充密钥状态失败（保留原原因）",
                             exc_info=True)
            logger.info("图片不可用（%s local_id=%s）：%s",
                        username, local_id, reason)
            return None, reason
        if not (data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG"
                or data[:3] == b"GIF"):
            logger.info("图片不可用（%s local_id=%s）：格式无法内嵌展示",
                        username, local_id)
            return None, "图片格式无法内嵌展示（非 jpg/png/gif）"
        return data, ""

    def get_voice_wav(self, username: str, local_id: int) -> Optional[bytes]:
        """取一条语音消息的可播放 WAV 音频；失败返回 None（原因记日志）。

        与转写共用同一条取音频链路（``_download_voice_silk``），解码用
        :func:`voice_asr.silk_to_wav`（只跑 SILK 解码器，不加载识别模型）。
        解码结果落盘 ``voicecache/{username}_{local_id}.wav``，下次直接
        读缓存。时长上限与转写口径一致（``asr.max_seconds``），超长不播。

        音频不出本机：只读本地微信库并通过本地接口返回。
        """
        try:
            local_id_int = int(local_id or 0)
        except (TypeError, ValueError):
            return None
        username = str(username or "")
        if not username or local_id_int <= 0:
            return None
        if self._wxdb is None:
            return None
        # 1) WAV 缓存命中（写缓存时已按 .part + 原子改名保证完整）
        wav_path = VOICE_CACHE_DIR / ("%s_%d.wav" % (username, local_id_int))
        try:
            if wav_path.is_file() and wav_path.stat().st_size > 44:
                return wav_path.read_bytes()
        except OSError:
            logger.exception("读取 WAV 播放缓存失败（%s）", wav_path)
        # 2) 取 SILK → 解码为 WAV（含时长上限校验）
        silk_path = self._download_voice_silk(username, local_id_int)
        if not silk_path:
            return None
        try:
            limit = voice_asr.get_transcriber().max_seconds()
        except Exception:  # 上限读取失败不影响播放能力
            limit = voice_asr.MAX_SECONDS_DEFAULT
        wav, err = voice_asr.silk_to_wav(silk_path, wav_path=wav_path,
                                         max_seconds=limit)
        if not wav:
            logger.info("语音播放取音频失败（%s local_id=%d）：%s",
                        username, local_id_int, err)
            return None
        return wav

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

    # ------------------------------------------------------------------
    # 图片识别队列可观测指标（只读，/api/status 与排障用）
    # ------------------------------------------------------------------
    @property
    def vision_queue_size(self) -> int:
        """待识别的图片任务数（queue.vision_pending 数据源）。"""
        try:
            return int(self._vision_queue.qsize())
        except Exception:       # 实测不会发生，防御性兜底
            return 0

    @property
    def vision_processed_total(self) -> int:
        """已处理完的图片任务总数（有结论 + 失败）。"""
        with self._vision_lock:
            return int(self._vision_ok + self._vision_fail)

    @property
    def vision_failed_total(self) -> int:
        """识别失败（坏图 / 取图失败 / 网关异常）的任务数。"""
        with self._vision_lock:
            return int(self._vision_fail)

    @property
    def image_retry_pending(self) -> int:
        """待补识别的图片数（原图未下载完、已排入退避重试队列的任务）。

        ``/api/status`` 的 ``queue.vision_retry_pending`` 数据源；读取失败按 0
        处理（状态接口绝不应因队列异常而 500）。
        """
        try:
            return int(self._image_retry.pending_count())
        except Exception:       # 防御性兜底：属性访问不应抛错
            logger.exception("读取图片补识别队列长度失败（按 0 处理）")
            return 0

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
        """探测微信主窗口是否可操作（只读 Win32，不创建 GUI 实例、不碰 UIA）。

        旧实现 ``FindWindowW(None, "微信")`` 是**标题精确匹配**，有两个实测
        误判（2026-11-25）：
        1. 微信 4.x 标题随未读消息数/当前会话变化（如「微信 (3)」），微信在跑
           却报「微信窗口不可用」，标题恢复后前端 5 秒轮询才自己变绿；
        2. 本机同时存在 WeChatAppEx 小程序全屏窗（Chrome_WidgetWin_0）也叫
           「微信」，精确匹配可能拿到错误的句柄。

        改为对齐 ``wechatauto.guia._looks_like_main_window`` 的判据：进程名
        weixin* 一票否决 + 标题含微信关键词 + 顶层可见 + 尺寸达标。每 5 秒被
        /api/status 调用，全程只读 Win32 调用（毫秒级）。失败时记下标题样本，
        下次复现可直接看到是不是标题变体导致，不再靠猜。
        """
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
        except Exception:                # 非 Windows / ctypes 不可用
            return False

        keywords = tuple(_WX_MAIN_TITLE_CANDIDATES) + ("Weixin",)

        def _proc_name(pid: int) -> str:
            if not pid:
                return ""
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
            if not handle:
                return ""    # 查询失败不否决，交给 Qt 类名兜底（同 guia 口径）
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if kernel32.QueryFullProcessImageNameW(
                        handle, 0, buf, ctypes.byref(size)):
                    return buf.value.rsplit("\\", 1)[-1].lower()
            except Exception:
                return ""
            finally:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
            return ""

        class _Rect(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        found = False
        samples: List[str] = []

        def _cb(hwnd, _lparam) -> bool:
            nonlocal found
            if not user32.IsWindowVisible(hwnd):
                return True
            title_buf = ctypes.create_unicode_buffer(256)
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buf, 256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            title, cls = title_buf.value.strip(), class_buf.value.strip()
            if not (title or cls):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = _proc_name(int(pid.value))
            qt_shell = cls.startswith("Qt") and "QWindowIcon" in cls
            # 标题含微信关键词即可；标题读不到时（跨完整性级别 WM_GETTEXT
            # 被拒）允许「Qt 外壳类名 + weixin 进程」兜底。
            if not any(k in title for k in keywords) and not (
                    qt_shell and proc.startswith("weixin")):
                return True
            # 进程身份一票否决：明确不是 weixin 进程的同名窗口一律不算
            # （EditPlus 打开「微信客服开发.txt」、WeChatAppEx 小程序窗）。
            if proc and not proc.startswith("weixin"):
                samples.append("%s@%s" % (title or cls, proc))
                return True
            # 进程查不到时只认 Qt 微信外壳类名，避免误绑其它程序的窗口。
            if not proc and not qt_shell:
                samples.append("%s@unknown" % (title or cls))
                return True
            # 真最小化（IsIconic=True）时 GetWindowRect 返回 Windows 的
            # -32000 伪矩形（约 160x28），尺寸门槛会误杀可用主窗口；
            # 发送链路用 SW_RESTORE 能唤起它，故此时跳过尺寸检查。
            # 托盘态（IsWindowVisible=False）已在入口过滤，行为不变。
            if not user32.IsIconic(hwnd):
                rect = _Rect()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if rect.right - rect.left < 400 or rect.bottom - rect.top < 300:
                    samples.append("%s@%s(小窗/最小化)" % (title or cls, proc or "?"))
                    return True
            found = True
            return False             # 命中即停止枚举

        try:
            cb_type = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                         ctypes.c_void_p, ctypes.c_void_p)
            cb = cb_type(_cb)
            user32.EnumWindows(cb, 0)
        except Exception:
            logger.debug("探测微信窗口失败（按不可用处理）", exc_info=True)
            return False

        if not found:
            # 失败时留下标题样本，便于下次复现时直接定位标题变体；
            # 微信退到托盘/最小化时每 5 秒都会失败，按分钟节流避免刷屏。
            now = time.time()
            if now - getattr(self, "_probe_miss_logged_at", 0) >= 60:
                self._probe_miss_logged_at = now
                logger.info("未探测到可操作的微信主窗口；顶层窗口样本=%s",
                            samples[:12])
        return found


def wait_seconds(seconds: float) -> None:
    """测试辅助：休眠指定秒数。"""
    time.sleep(seconds)

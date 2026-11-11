# -*- coding: utf-8 -*-
"""全局路径与可配置常量（共享约定见 docs/技术方案与任务分解.md 第 8 节）。"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # kefu-system/
DATA_DIR = BASE_DIR / "data"                               # 运行数据目录
DB_PATH = DATA_DIR / "kefu.sqlite3"                        # 业务 SQLite
SECRET_KEY_PATH = DATA_DIR / "secret.key"                  # Fernet 密钥文件
LOG_PATH = DATA_DIR / "kefu.log"                           # 滚动日志
IMG_CACHE_DIR = DATA_DIR / "imgcache"                      # 图片解密缓存（反馈 2）
# 语音消息落盘缓存（微信 SILK 原始文件，转写前临时解码）
VOICE_CACHE_DIR = DATA_DIR / "voicecache"
# 离线语音识别模型目录（SenseVoice-Small：model.int8.onnx + tokens.txt）
ASR_MODEL_DIR = DATA_DIR / "asr_models"
# 微信 SILK v3 解码器（32 位 exe，仓库内置；缺失时可从镜像下载）
SILK_DECODER_PATH = BASE_DIR / "tools" / "silk_v3_decoder.exe"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"             # 前端构建产物

# 微信数据库解密缓存（SQLCipher 解密副本 + 页哈希 + 密钥缓存）默认目录。
# 放在客服系统运行目录下，避免落到 %TEMP% 被系统/清理软件删除（删了要全量
# 重新解密，且需重新提取密钥）。可在设置页改为其它磁盘（如 D:\wxcache）。
CACHE_DIR_DEFAULT = DATA_DIR / "wechat_cache"
LEGACY_CACHE_ROOT = Path(os.environ.get("TEMP") or os.environ.get("TMP")
                         or "/tmp") / "wechatauto_db"


def resolve_cache_dir(raw: str | None = None) -> Path:
    """把配置值规整为绝对路径（空值 / 相对路径 / ~ / 环境变量均可）。

    相对路径按 kefu-system/ 解析，避免受进程工作目录影响。
    """
    text = str(raw or "").strip()
    if not text:
        return CACHE_DIR_DEFAULT
    text = os.path.expandvars(os.path.expanduser(text))
    path = Path(text)
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        return path.resolve()
    except OSError:      # 盘符不存在等极端情况：返回未解析路径
        return path


def check_cache_dir(raw: str | None = None) -> tuple[bool, str, Path]:
    """校验缓存目录可用性并确保目录存在，返回 (是否可用, 中文原因, 绝对路径)。

    Windows 上「目录不存在 / 只读 / 是文件」都会在真正写入缓存时才炸，
    且现场是后台重建线程，报错不明显，所以在保存配置前就验证一次。
    """
    path = resolve_cache_dir(raw)
    if len(str(path)) > 240:
        return False, "路径过长（超过 240 个字符）", path
    if path.exists() and not path.is_dir():
        return False, "该路径已存在且不是文件夹", path
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建目录：{exc}", path
    probe = path / f".kefu_write_test_{os.getpid()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return False, f"目录不可写：{exc}", path
    return True, "", path


def dir_usage(path: Path | str) -> tuple[int, int]:
    """统计目录占用 (字节数, 文件数)。缓存目录是平铺文件，只统计一层。"""
    total = 0
    count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                        count += 1
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return total, count

# ---------------------------------------------------------------------------
# 管线常量（窗口允许 settings 覆盖：analysis.merge_window_sec / analysis.opp_merge_window_sec）
# ---------------------------------------------------------------------------
MERGE_WINDOW_SEC_DEFAULT = 15      # 分析微窗口默认 15 秒（发送人静默该时长即触发 AI 分析）
OPP_MERGE_WINDOW_SEC_DEFAULT = 180  # 商机合并窗口默认 180 秒（同一人再次命中合并为一条）
MAX_OPPORTUNITY_SPAN_SEC = 1800    # 单条商机最长时间跨度（window_start 距今超过则不再合并，防无限续命）
AGGREGATOR_FORCE_FLUSH_FACTOR = 10  # 缓冲区首条消息年龄超过 微窗口×该倍数 时强制 flush（安全阀）
QUEUE_MAX = 500                    # 有界分析队列长度（满则丢弃并计数告警）
WORKER_COUNT = 2                   # 分析 worker 线程数
CONTEXT_COUNT = 5                  # 商机详情上下文条数（前后各 N 条）
WATERMARK_FLUSH_SEC = 30           # watermark 定期刷回间隔（秒）
RETRY_TIMES = 2                    # AI 调用失败重试次数
RETRY_BACKOFF_BASE = 1.0           # 重试退避基数（秒），第 n 次重试退避 base*2^(n-1)
SESSION_SEARCH_LIMIT_DEFAULT = 20  # 会话搜索默认条数
SESSION_CACHE_TTL_SEC = 60         # 全量联系人/会话列表内存缓存 TTL（秒）
MSG_QUERY_RETRY_TIMES = 2          # 微信消息查询瞬时失败重试次数
MSG_QUERY_RETRY_INTERVAL_SEC = 1.5 # 消息查询重试间隔（秒）
CORRECTION_EXAMPLES_MAX = 20       # 分析修正示例上限（FIFO 淘汰最旧）
GROUP_MEMBER_CACHE_TTL_SEC = 600   # 群成员列表缓存 TTL（秒，发送人显示名解析用）
MAX_MONITOR_TARGETS = 200          # 监控对象数量上限兜底值（settings 无 monitor.max_targets 时使用）
MONITOR_MAX_TARGETS_MIN = 10       # monitor.max_targets 允许下限
MONITOR_MAX_TARGETS_MAX = 2000     # monitor.max_targets 允许上限
# 关闭 AI 思考模式（实测 GLM-5.x 默认思考会让单条消息耗时 27s→3s 级别；
# 网关不支持该参数时 AIClient 自动降级，不会报错）
AI_DISABLE_THINKING_DEFAULT = True

# ---------------------------------------------------------------------------
# settings 表配置键（共享约定，前端与后端共同遵守）
# ---------------------------------------------------------------------------
KEY_AI_BASE_URL = "ai.base_url"
KEY_AI_API_KEY_ENC = "ai.api_key_enc"      # Fernet 密文
KEY_AI_MODEL = "ai.model"
KEY_AI_TIMEOUT_SEC = "ai.timeout_sec"
KEY_AI_TEMPERATURE = "ai.temperature"
KEY_AI_ENABLED = "ai.enabled"              # 是否使用 AI 接口（0=用轻量筛选规则判定商机）
KEY_ANALYSIS_STRICTNESS = "analysis.strictness"
KEY_ANALYSIS_MIN_CONFIDENCE = "analysis.min_confidence"
KEY_ANALYSIS_PRESCREEN_ENABLED = "analysis.prescreen_enabled"
KEY_ANALYSIS_PRESCREEN_KEYWORDS = "analysis.prescreen_keywords"
KEY_ANALYSIS_PRESCREEN_REGEX = "analysis.prescreen_regex"
KEY_ANALYSIS_MERGE_WINDOW = "analysis.merge_window_sec"
KEY_ANALYSIS_OPP_MERGE_WINDOW = "analysis.opp_merge_window_sec"
KEY_ANALYSIS_CORRECTION_EXAMPLES = "analysis.correction_examples"  # JSON 数组字符串
KEY_NOTIFY_SOUND = "notify.sound_enabled"
# 语音消息自动转文字（本地离线识别，音频绝不上传外部服务）
KEY_ASR_ENABLED = "asr.enabled"
# 单条语音转写时长上限（秒）：超长语音直接放弃，避免长时间占用识别线程
KEY_ASR_MAX_SECONDS = "asr.max_seconds"
KEY_MONITOR_MAX_TARGETS = "monitor.max_targets"  # 监控对象数量上限（10~2000，默认 200）
KEY_COMPLIANCE_CONFIRMED = "compliance.confirmed"
KEY_RUNTIME_WM_SAVED_AT = "runtime.watermark_saved_at"  # 内部键：最近 watermark 刷回时间
KEY_RUNTIME_MW_MIGRATED = "runtime.merge_window_migrated"  # 内部键：微窗口默认值迁移标记
# 内部键：创建时间由 UTC 改为本地时间的迁移标记（一次性，防重复平移）
KEY_RUNTIME_LOCALTIME_MIGRATED = "runtime.created_at_localtime"
# 内部键：把 analysis_logs 已有 token 用量回填到 ai_usage 的一次性标记
KEY_RUNTIME_AI_USAGE_BACKFILLED = "runtime.ai_usage_backfilled"
# 微信数据库解密缓存目录（空 = 默认 DATA_DIR/wechat_cache；相对路径按 kefu-system/ 解析）
KEY_RUNTIME_CACHE_DIR = "runtime.cache_dir"
# 内部键：最近一次手动清理记录（JSON：table/deleted/keep_days/cutoff/at）。
# 落库留痕，不依赖日志文件——日志轮转失败时仍能查出「数据是被谁清理掉的」。
KEY_RUNTIME_LAST_PURGE = "runtime.last_purge"

# ---------------------------------------------------------------------------
# 默认配置值
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, str] = {
    KEY_AI_BASE_URL: "",
    KEY_AI_MODEL: "",
    KEY_AI_TIMEOUT_SEC: "60",
    KEY_AI_TEMPERATURE: "0.2",
    KEY_AI_ENABLED: "1",
    KEY_ANALYSIS_STRICTNESS: "balanced",
    KEY_ANALYSIS_MIN_CONFIDENCE: "60",
    KEY_ANALYSIS_PRESCREEN_ENABLED: "0",
    KEY_ANALYSIS_PRESCREEN_KEYWORDS: "采购,求购,报价,加工",
    KEY_ANALYSIS_PRESCREEN_REGEX: "",
    KEY_ANALYSIS_MERGE_WINDOW: str(MERGE_WINDOW_SEC_DEFAULT),
    KEY_ANALYSIS_OPP_MERGE_WINDOW: str(OPP_MERGE_WINDOW_SEC_DEFAULT),
    KEY_ANALYSIS_CORRECTION_EXAMPLES: "[]",
    KEY_NOTIFY_SOUND: "0",
    KEY_ASR_ENABLED: "1",          # 语音消息自动转文字（本地离线识别）
    KEY_ASR_MAX_SECONDS: "180",    # 单条语音转写时长上限（秒）
    KEY_MONITOR_MAX_TARGETS: str(MAX_MONITOR_TARGETS),
    KEY_COMPLIANCE_CONFIRMED: "0",
    KEY_RUNTIME_CACHE_DIR: "",     # 空 = 使用 CACHE_DIR_DEFAULT
    KEY_RUNTIME_LAST_PURGE: "",    # 空 = 尚未执行过手动清理（JSON 字符串）
}

# 判定严格度取值
STRICTNESS_VALUES = ("lenient", "balanced", "strict")
# 商机状态取值（handled = 用户已在微信里自行处理）
OPPORTUNITY_STATUS_VALUES = ("pending", "replied", "ignored", "handled")

# 日志配置
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

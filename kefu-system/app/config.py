# -*- coding: utf-8 -*-
"""全局路径与可配置常量（共享约定见 docs/技术方案与任务分解.md 第 8 节）。"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
# APP_ROOT：只读资源根（随版本走的 app/<版本>/ 或开发态 kefu-system/）。
# DATA_DIR：可写数据根，P1 三层布局里由启动器注入 KEFU_DATA_DIR 指向安装根的
#           data/，**升级永不覆盖**；缺省回退到 APP_ROOT/data（P0 布局兼容）。
# ASSETS_DIR：只读重资源根（228MB ASR 模型、SILK 解码器、ffmpeg），放稳定的
#             runtime/ 层，不随版本升级重下；缺省回退到各自的 P0 位置。
APP_ROOT = Path(os.environ.get("KEFU_APP_ROOT", "").strip() or
                Path(__file__).resolve().parent.parent)
BASE_DIR = APP_ROOT                                        # 历史别名，等于 APP_ROOT
_data_override = os.environ.get("KEFU_DATA_DIR", "").strip()
DATA_DIR = Path(_data_override) if _data_override else (BASE_DIR / "data")
_assets_override = os.environ.get("KEFU_ASSETS_DIR", "").strip()
ASSETS_DIR = Path(_assets_override) if _assets_override else None

DB_PATH = DATA_DIR / "kefu.sqlite3"                        # 业务 SQLite
SECRET_KEY_PATH = DATA_DIR / "secret.key"                  # Fernet 密钥文件
LOG_PATH = DATA_DIR / "kefu.log"                           # 滚动日志
IMG_CACHE_DIR = DATA_DIR / "imgcache"                      # 图片解密缓存（反馈 2）
# 授权许可证（服务端签发的签名文件）与本地状态（last_ok_at / max_seen_ts）
LICENSE_FILE = DATA_DIR / "license.json"
LICENSE_STATE_FILE = DATA_DIR / "license.state.json"
# 语音消息落盘缓存（微信 SILK 原始文件，转写前临时解码）
VOICE_CACHE_DIR = DATA_DIR / "voicecache"
# 离线语音识别模型目录（SenseVoice-Small：model.int8.onnx + tokens.txt）
# P1：优先 ASSETS_DIR/asr_models（runtime 层，升级不动）；回退 DATA_DIR/asr_models
ASR_MODEL_DIR = (ASSETS_DIR / "asr_models") if ASSETS_DIR else (DATA_DIR / "asr_models")
# 微信 SILK v3 解码器（仓库内置；缺失时可从镜像下载）
# P1：优先 ASSETS_DIR/silk_v3_decoder.exe；回退 APP_ROOT/tools（P0 布局）
SILK_DECODER_PATH = ((ASSETS_DIR / "silk_v3_decoder.exe") if ASSETS_DIR
                     else (APP_ROOT / "tools" / "silk_v3_decoder.exe"))
FRONTEND_DIST = APP_ROOT / "frontend" / "dist"             # 前端构建产物

# 交付版数据库初始化预设（seed）：鉴权生产地址 + 预置商机类型。
# 由托盘启动器注入 KEFU_DELIVERY_SEED 指向 bundle 根的 delivery_seed.json；
# 开发/测试态不设该环境变量 → 文件不存在 → 完全不生效（DB 仍走内置默认）。
_delivery_seed = os.environ.get("KEFU_DELIVERY_SEED", "").strip()
DELIVERY_SEED_FILE = Path(_delivery_seed) if _delivery_seed else None

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
# 图片识别队列（批次 6 需求 5 异步化）：视觉模型单张实测 10~15 秒（超时
# 上限 60 秒），绝不能放在 Listener 回调线程里同步跑，否则后续文本消息的
# 入库会被推迟几十秒。监听线程只入队，识别由独立 worker 并发完成。
VISION_QUEUE_MAX = 200             # 图片识别队列上限（满则跳过识别并记日志，绝不阻塞监听）
VISION_WORKERS = 2                 # 图片识别 worker 线程数（纯 I/O 等待，2 个足够且不吃 CPU）
VISION_QUEUE_WARN = 50             # 队列积压告警阈值（超过时限频 warn 一次）
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
# 版本号（唯一真源：仓库根 version.txt）
# ---------------------------------------------------------------------------
# 发版只改 version.txt，并用 `python kefu-system/tools/version_sync.py --bump X.Y.Z`
# 同步其余位置。这里运行时直接读文件，避免 APP_VERSION 变成第二份真源。
# 打包后 version.txt 随包落在安装根 / app\<版本>\，也可用 KEFU_VERSION_FILE 指定。
# 只有「version.txt 完全找不到」时才用兜底值；兜底值同样被 version_sync.py 校验。
_FALLBACK_VERSION = "1.3.4"


def _read_app_version() -> str:
    """按「显式指定 → KEFU_APP_ROOT → 打包布局 → 开发态仓库根」顺序读版本号。"""
    import re
    version_re = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")
    candidates: list[Path] = []
    override = os.environ.get("KEFU_VERSION_FILE", "").strip()
    if override:
        candidates.append(Path(override))
    app_root = os.environ.get("KEFU_APP_ROOT", "").strip()
    if app_root:
        candidates.append(Path(app_root) / "version.txt")
    here = Path(__file__).resolve()
    candidates.extend([
        here.parent / "version.txt",           # 打包：app/version.txt
        here.parent.parent / "version.txt",    # 打包：kefu-system/version.txt
        here.parents[2] / "version.txt",       # 开发态：仓库根/version.txt
    ])
    for path in candidates:
        try:
            lines = path.read_bytes().decode("utf-8").splitlines()
        except OSError:
            continue
        if lines and version_re.match(lines[0].strip()):
            return lines[0].strip()
    return _FALLBACK_VERSION


APP_VERSION = _read_app_version()

# ---------------------------------------------------------------------------
# 授权（license）相关常量
# ---------------------------------------------------------------------------
# 模拟授权服务器默认地址（真实环境用 license.server_url 或环境变量覆盖）
DEFAULT_LICENSE_SERVER_URL = "http://127.0.0.1:8799"
LICENSE_DEFAULT_GRACE_DAYS = 7        # 离线宽限默认天数
LICENSE_DEFAULT_REFRESH_HOURS = 6     # 联网确认默认间隔（小时）

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
# 是否过滤自己发送的消息（默认 "1"=过滤：不进入商机分析，仅记 skipped 日志）
KEY_ANALYSIS_FILTER_SELF = "analysis.filter_self_messages"
KEY_NOTIFY_SOUND = "notify.sound_enabled"
# 语音消息自动转文字（本地离线识别，音频绝不上传外部服务）
KEY_ASR_ENABLED = "asr.enabled"
# 单条语音转写时长上限（秒）：超长语音直接放弃，避免长时间占用识别线程
KEY_ASR_MAX_SECONDS = "asr.max_seconds"
# 图片内容识别（视觉模型）：识别图片里的车辆/汽车配件，提取车架号，
# 识别为车辆/配件时按「卖货」方向产出商机（批次 6 需求 5）
KEY_IMAGE_VISION_ENABLED = "image.vision_enabled"
# 视觉模型名（空 = 沿用 ai.model：多数多模态网关文本/视觉同一个模型）
KEY_IMAGE_VISION_MODEL = "image.vision_model"
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
# 电脑授权（license）：**默认强制启用**（机器码绑定 + 到期校验）。
# 取值只有两种语义（v2，fail-closed）：
#   - 等于厂商测试口令（明文不随程序分发，代码里只存 SHA256）→ 测试模式，
#     关闭授权校验，界面显示醒目横幅；
#   - 其它任何值（"1" / "0" / "false" / 空）→ 一律需要授权。
#     改数据库/配置文件为 0 不再能绕过授权。
# 交付脚本建议再加 `--license`（写入环境变量 KEFU_LICENSE_MODE=enforce），
# 这样连测试口令都失效，只能走正规激活。
# ⚠️ 改这里只影响**新建**的数据库：DEFAULT_SETTINGS 是 INSERT OR IGNORE 播种，
# 已有库里那行 license.enabled 会一直盖住代码默认值。切模式请用
# `python run.py --license-on` / `--license-dev`。
KEY_LICENSE_ENABLED = "license.enabled"
# 授权服务器地址（真实后台部署后改这里；环境变量 KEFU_LICENSE_SERVER 优先）
KEY_LICENSE_SERVER_URL = "license.server_url"
# 预置激活码（批量部署时可由部署脚本写入，客户无需手输；空 = 界面输入）
KEY_LICENSE_ACTIVATION_CODE = "license.activation_code"
# 联网确认间隔（小时）：距上次成功确认超过该值即后台静默刷新一次
KEY_LICENSE_REFRESH_INTERVAL = "license.refresh_interval_hours"
# 本地兜底离线宽限天数（许可证 payload 带 grace_days 时以许可证为准）
KEY_LICENSE_GRACE_DAYS = "license.grace_days"

# 群消息总结（预算双口径 / 超限双策略 / 定时 P1 / 保留天数）
KEY_SUMMARY_BUDGET_MODE = "summary.budget_mode"
KEY_SUMMARY_BUDGET_MONEY = "summary.budget_money"
KEY_SUMMARY_BUDGET_TOKENS = "summary.budget_tokens"
KEY_SUMMARY_OVER_LIMIT_ACTION = "summary.over_limit_action"
KEY_SUMMARY_TRUNCATE_RECENT_N = "summary.truncate_recent_n"
KEY_SUMMARY_SCHEDULE_ENABLED = "summary.schedule_enabled"
KEY_SUMMARY_SCHEDULE_CONFIGS = "summary.schedule_configs"
KEY_SUMMARY_PROMPT_TEMPLATES = "summary.prompt_templates"   # P1
KEY_RETENTION_SUMMARIES_DAYS = "retention.summaries_days"

# 预算口径 / 超限策略的合法取值（与 schemas.STYLE_VALUES 分属不同概念）
BUDGET_MODE_VALUES = ("money", "tokens")
OVER_LIMIT_ACTION_VALUES = ("block", "truncate_recent")
SUMMARY_SCHEDULE_TIME_DEFAULT = "18:00"   # P1：每日一次，默认 18:00

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
    KEY_ANALYSIS_FILTER_SELF: "1",   # 过滤自己发送的消息（默认开启）
    KEY_NOTIFY_SOUND: "0",
    KEY_ASR_ENABLED: "1",          # 语音消息自动转文字（本地离线识别）
    KEY_ASR_MAX_SECONDS: "180",    # 单条语音转写时长上限（秒）
    KEY_IMAGE_VISION_ENABLED: "1",  # 图片内容识别（视觉模型，默认开启）
    KEY_IMAGE_VISION_MODEL: "",     # 空 = 沿用 ai.model
    KEY_MONITOR_MAX_TARGETS: str(MAX_MONITOR_TARGETS),
    KEY_COMPLIANCE_CONFIRMED: "0",
    KEY_RUNTIME_CACHE_DIR: "",     # 空 = 使用 CACHE_DIR_DEFAULT
    KEY_RUNTIME_LAST_PURGE: "",    # 空 = 尚未执行过手动清理（JSON 字符串）
    # 授权：默认值只对新建数据库（settings 表）生效；已有库用 --license /
    # 环境变量 KEFU_LICENSE_ENABLED=1 覆盖，或改 settings 表里的 license.enabled。
    KEY_LICENSE_ENABLED: "1",
    KEY_LICENSE_SERVER_URL: DEFAULT_LICENSE_SERVER_URL,
    KEY_LICENSE_ACTIVATION_CODE: "",
    KEY_LICENSE_REFRESH_INTERVAL: str(LICENSE_DEFAULT_REFRESH_HOURS),
    KEY_LICENSE_GRACE_DAYS: str(LICENSE_DEFAULT_GRACE_DAYS),
    # 群消息总结：两值分开存，切换口径无需换算；定时总开关默认关（不打扰）
    KEY_SUMMARY_BUDGET_MODE: "money",
    KEY_SUMMARY_BUDGET_MONEY: "1.0",
    KEY_SUMMARY_BUDGET_TOKENS: "60000",
    KEY_SUMMARY_OVER_LIMIT_ACTION: "block",
    KEY_SUMMARY_TRUNCATE_RECENT_N: "200",
    KEY_SUMMARY_SCHEDULE_ENABLED: "0",
    KEY_SUMMARY_SCHEDULE_CONFIGS: "[]",
    KEY_SUMMARY_PROMPT_TEMPLATES: "[]",
    KEY_RETENTION_SUMMARIES_DAYS: "180",
}

# 判定严格度取值
STRICTNESS_VALUES = ("lenient", "balanced", "strict")
# 商机状态取值（handled = 用户已在微信里自行处理）
OPPORTUNITY_STATUS_VALUES = ("pending", "replied", "ignored", "handled")

# 日志配置
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

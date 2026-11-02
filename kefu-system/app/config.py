# -*- coding: utf-8 -*-
"""全局路径与可配置常量（共享约定见 docs/技术方案与任务分解.md 第 8 节）。"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # kefu-system/
DATA_DIR = BASE_DIR / "data"                               # 运行数据目录
DB_PATH = DATA_DIR / "kefu.sqlite3"                        # 业务 SQLite
SECRET_KEY_PATH = DATA_DIR / "secret.key"                  # Fernet 密钥文件
LOG_PATH = DATA_DIR / "kefu.log"                           # 滚动日志
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"             # 前端构建产物

# ---------------------------------------------------------------------------
# 管线常量（合并窗口允许 settings 覆盖：analysis.merge_window_sec）
# ---------------------------------------------------------------------------
MERGE_WINDOW_SEC_DEFAULT = 300     # 合并窗口默认 5 分钟
QUEUE_MAX = 500                    # 有界分析队列长度（满则丢弃并计数告警）
WORKER_COUNT = 2                   # 分析 worker 线程数
CONTEXT_COUNT = 5                  # 商机详情上下文条数（前后各 N 条）
WATERMARK_FLUSH_SEC = 30           # watermark 定期刷回间隔（秒）
RETRY_TIMES = 2                    # AI 调用失败重试次数
RETRY_BACKOFF_BASE = 1.0           # 重试退避基数（秒），第 n 次重试退避 base*2^(n-1)
SESSION_SEARCH_LIMIT_DEFAULT = 20  # 会话搜索默认条数

# ---------------------------------------------------------------------------
# settings 表配置键（共享约定，前端与后端共同遵守）
# ---------------------------------------------------------------------------
KEY_AI_BASE_URL = "ai.base_url"
KEY_AI_API_KEY_ENC = "ai.api_key_enc"      # Fernet 密文
KEY_AI_MODEL = "ai.model"
KEY_AI_TIMEOUT_SEC = "ai.timeout_sec"
KEY_AI_TEMPERATURE = "ai.temperature"
KEY_ANALYSIS_STRICTNESS = "analysis.strictness"
KEY_ANALYSIS_MIN_CONFIDENCE = "analysis.min_confidence"
KEY_ANALYSIS_PRESCREEN_ENABLED = "analysis.prescreen_enabled"
KEY_ANALYSIS_PRESCREEN_KEYWORDS = "analysis.prescreen_keywords"
KEY_ANALYSIS_PRESCREEN_REGEX = "analysis.prescreen_regex"
KEY_ANALYSIS_MERGE_WINDOW = "analysis.merge_window_sec"
KEY_NOTIFY_SOUND = "notify.sound_enabled"
KEY_COMPLIANCE_CONFIRMED = "compliance.confirmed"
KEY_RUNTIME_WM_SAVED_AT = "runtime.watermark_saved_at"  # 内部键：最近 watermark 刷回时间

# ---------------------------------------------------------------------------
# 默认配置值
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, str] = {
    KEY_AI_BASE_URL: "",
    KEY_AI_MODEL: "",
    KEY_AI_TIMEOUT_SEC: "60",
    KEY_AI_TEMPERATURE: "0.2",
    KEY_ANALYSIS_STRICTNESS: "balanced",
    KEY_ANALYSIS_MIN_CONFIDENCE: "60",
    KEY_ANALYSIS_PRESCREEN_ENABLED: "0",
    KEY_ANALYSIS_PRESCREEN_KEYWORDS: "采购,求购,报价,加工",
    KEY_ANALYSIS_PRESCREEN_REGEX: "",
    KEY_ANALYSIS_MERGE_WINDOW: str(MERGE_WINDOW_SEC_DEFAULT),
    KEY_NOTIFY_SOUND: "0",
    KEY_COMPLIANCE_CONFIRMED: "0",
}

# 判定严格度取值
STRICTNESS_VALUES = ("lenient", "balanced", "strict")
# 商机状态取值
OPPORTUNITY_STATUS_VALUES = ("pending", "replied", "ignored")

# 日志配置
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

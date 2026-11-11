# -*- coding: utf-8 -*-
"""Pydantic 请求/响应模型（与前端 src/api/types.ts 字段一一对应）。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 系统
# ---------------------------------------------------------------------------
class WechatStatus(BaseModel):
    """微信接入状态。"""
    alive: bool = False
    visible: bool = False


class QueueStatus(BaseModel):
    size: int = 0
    max: int = 0
    dropped_total: int = 0


class WorkerStatus(BaseModel):
    busy: int = 0
    max: int = 0


class MonitorQuota(BaseModel):
    """监控对象配额（used/max，设置页双栏顶部提示用）。"""
    used: int = 0
    max: int = 0


class AnalysisHealth(BaseModel):
    """AI 分析健康度（内存态，pipeline 维护；页面刷新后仍可展示异常）。"""
    last_error: str = ""               # 最近一次失败的友好错误摘要（成功后清空）
    consecutive_failures: int = 0      # 连续失败次数（成功后归零）


class CacheShardStatus(BaseModel):
    """单个消息分片的解密缓存健康度（可选，仅内部排查/运营可见）。"""
    rel: str = ""                        # 分片相对路径
    stale_seconds: Optional[float] = None  # 距上次成功更新秒数（None = 从未成功）
    last_ok_at: str = ""                 # 最近一次成功更新时刻（ISO8601）
    last_error: str = ""                 # 最近一次失败原因（空 = 无失败）
    fail_streak: int = 0                 # 连续失败次数
    rebuilding: bool = False             # 当前是否正在后台重建


class StatusOut(BaseModel):
    listener_running: bool = False
    monitored_count: int = 0
    wechat: WechatStatus = Field(default_factory=WechatStatus)
    queue: QueueStatus = Field(default_factory=QueueStatus)
    worker: WorkerStatus = Field(default_factory=WorkerStatus)
    monitor_quota: MonitorQuota = Field(default_factory=MonitorQuota)
    analysis_health: AnalysisHealth = Field(default_factory=AnalysisHealth)
    ai_configured: bool = False
    # 是否启用 AI 接口（False = 轻量筛选模式，商机由关键词/正则规则产生）
    ai_enabled: bool = True
    watermark_saved_at: str = ""
    degraded_reason: str = ""          # 微信接入降级原因（空 = 正常）
    unread_count: int = 0
    today_cost: float = 0.0            # 今日 AI 估算费用（元，反馈 13）
    # 解密缓存健康度（新增追加字段，默认空列表，不影响既有前端契约）
    cache: List[CacheShardStatus] = Field(default_factory=list)




class SessionItemOut(BaseModel):
    username: str
    display_name: str
    kind: str                          # group / friend


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------
class MessageOut(BaseModel):
    local_id: int = 0
    type: str = ""
    type_name: str = ""
    sender_id: int = 0
    sender_name: str = ""
    create_time: str = ""              # ISO8601
    content: str = ""
    sort_seq: int = 0


# ---------------------------------------------------------------------------
# 监控对象
# ---------------------------------------------------------------------------
class MonitorTargetOut(BaseModel):
    id: int
    username: str
    display_name: str
    kind: str
    enabled: int


class MonitorTargetCreate(BaseModel):
    username: str = Field(min_length=1)
    display_name: str = ""             # 前端候选列表已知的显示名快照；空则由后端解析


class MonitorTargetUpdate(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# 商机类型
# ---------------------------------------------------------------------------
class OpportunityTypeOut(BaseModel):
    id: int
    name: str
    description: str
    enabled: int
    updated_at: str = ""


class OpportunityTypeCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    enabled: bool = True


class OpportunityTypeUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# AI / 分析设置
# ---------------------------------------------------------------------------
class AISettingsOut(BaseModel):
    base_url: str = ""
    api_key_masked: str = ""
    model: str = ""
    timeout_sec: int = 60
    temperature: float = 0.2
    disable_thinking: bool = True      # 关闭思考模式（显著降低响应延迟）
    # 是否使用 AI 接口；False 时不调用 AI，改用「轻量筛选」关键词/正则判定商机
    enabled: bool = True


class AISettingsUpdate(BaseModel):
    base_url: str = ""
    api_key: str = ""                  # 留空 = 不变更
    model: str = ""
    timeout_sec: int = Field(default=60, ge=5, le=600)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    disable_thinking: bool = True      # 关闭思考模式（默认开启）
    enabled: bool = True               # 是否使用 AI 接口（关闭=轻量筛选模式）
    confirm_compliance: bool = False   # 首次保存需勾选合规知情确认


class AITestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int = 0
    model: str = ""


class AnalysisSettingsOut(BaseModel):
    strictness: str = "balanced"
    min_confidence: int = 60
    prescreen_enabled: bool = False
    prescreen_keywords: str = ""
    prescreen_regex: str = ""
    merge_window_sec: int = 15         # 分析微窗口：发送人静默 N 秒即触发 AI 分析
    opp_merge_window_sec: int = 180    # 商机合并窗口：同一人 N 秒内再次命中合并为一条
    correction_examples: List[str] = Field(default_factory=list)  # 漏判修正示例（最多 20 条）


class AnalysisSettingsUpdate(BaseModel):
    strictness: str = Field(pattern="^(lenient|balanced|strict)$")
    min_confidence: int = Field(ge=0, le=100)
    prescreen_enabled: bool
    prescreen_keywords: str = ""
    prescreen_regex: str = ""
    merge_window_sec: int = Field(default=15, ge=5, le=600)
    opp_merge_window_sec: int = Field(default=180, ge=30, le=1800)
    # None = 不变更（防止部分更新的旧调用把已有示例意外清空）；[] = 显式清空
    correction_examples: Optional[List[str]] = None


class NotifyUpdate(BaseModel):
    sound_enabled: bool


class MonitorSettingsOut(BaseModel):
    """监控设置（当前仅暴露监控对象数量上限）。"""
    max_targets: int = Field(default=200, ge=10, le=2000)


class MonitorSettingsUpdate(BaseModel):
    max_targets: int = Field(ge=10, le=2000)


# ---------------------------------------------------------------------------
# 历史数据管理（反馈 7）/ AI 账单（反馈 13）
# ---------------------------------------------------------------------------
class RetentionOut(BaseModel):
    """历史数据保留配置（两表分开管理）。

    天数语义统一为「保留最近 N 个自然日（含今天）」：N=1 只保留今天，
    N=2 保留昨天与今天，以此类推。
    """
    logs_days: int = Field(default=90, ge=1, le=3650)    # analysis_logs 保留最近 N 天（含今天）
    opportunities_days: int = Field(default=365, ge=1, le=3650)  # 商机保留最近 N 天（含今天）


class RetentionUpdate(BaseModel):
    logs_days: Optional[int] = Field(default=None, ge=1, le=3650)
    opportunities_days: Optional[int] = Field(default=None, ge=1, le=3650)


class PurgeRequest(BaseModel):
    """立即清理请求。

    keep_days 语义为「保留最近 N 个自然日（含今天）」：N=1 只保留今天。
    dry_run=True 时只预览将被删除的条数（不写库、不可逆操作前先给用户看），
    dry_run=False（默认）才真正执行删除。
    """
    table: str = Field(pattern="^(analysis_logs|opportunities)$")
    keep_days: int = Field(ge=1, le=3650)
    dry_run: bool = False


class CacheDirOut(BaseModel):
    """微信数据库解密缓存目录信息。"""
    dir: str = ""                 # 当前配置解析后的绝对路径
    default_dir: str = ""         # 默认目录（客服系统运行目录下）
    configured: str = ""          # 配置值原文（空 = 默认）
    is_default: bool = True
    size_bytes: int = 0
    file_count: int = 0
    active_dir: str = ""          # 监听实例实际使用的目录
    active: bool = False          # 是否已在新目录上运行（false = 需重启）
    legacy_dirs: List[str] = []   # 旧版 %TEMP%\wechatauto_db 下的目录


class CacheDirUpdate(BaseModel):
    """修改缓存目录（空字符串 = 恢复默认目录）。"""
    dir: str = ""


class PurgeResult(BaseModel):
    """清理结果（dry_run=True 时 deleted 为「将删除条数」，未真正删除）。

    cutoff 为由 SQLite 按本地时间计算的截断点（YYYY-MM-DD HH:MM:SS），
    供前端在确认对话框中明确展示「将删除早于某时刻的记录」。
    """
    table: str
    deleted: int
    dry_run: bool = False
    cutoff: str = ""


class BillingDayOut(BaseModel):
    date: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


class BillingOut(BaseModel):
    """AI 账单估算（按设置的输入/输出单价计算，元）。"""
    unit: str = "per_k"                       # per_k=元/千tokens per_m=元/百万tokens
    input_price: float = 0.0
    output_price: float = 0.0
    today: BillingDayOut = Field(default_factory=lambda: BillingDayOut(date=""))
    series: List[BillingDayOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 商机
# ---------------------------------------------------------------------------
class OpportunityListItem(BaseModel):
    id: int
    session_username: str = ""
    session_name: str = ""
    sender_wxid: str = ""
    sender_name: str = ""
    window_end: str = ""
    type_id: Optional[int] = None
    type_name: str = ""
    summary: str = ""
    key_quote: str = ""                # 关键原文摘录（列表页「消息原文」列）
    confidence: int = 0
    direction: str = ""                # 买/卖货方向（反馈 4）：buy/sell/''=不明
    status: str = "pending"
    created_at: str = ""
    # 未读标识（内存态：新商机进入即未读，打开详情或点击行内「未读」后清除）
    unread: bool = False


class OpportunityListOut(BaseModel):
    total: int
    items: List[OpportunityListItem]


class OpportunityDetailOut(OpportunityListItem):
    sender_name: str = ""
    window_start: str = ""
    merged_count: int = 1
    raw_text: str = ""
    # 原文展示版：群消息每行发送者 ID 已替换为显示名（前端展示用）
    raw_text_display: str = ""
    key_quote: str = ""
    reply_content: Optional[str] = None
    replied_at: Optional[str] = None
    # 上下文已拆分为独立接口 GET /api/opportunities/{id}/context
    # （详情接口不查微信库，秒开；上下文区域独立加载态）


class OpportunityContextOut(BaseModel):
    """商机上下文（独立异步接口；error 非空 = 微信库读取失败的用户可读原因）。"""
    items: List[MessageOut] = Field(default_factory=list)
    error: str = ""


class OpportunityStatusUpdate(BaseModel):
    status: str = Field(pattern="^(pending|replied|ignored|handled)$")


class QuoteRef(BaseModel):
    local_id: int


class ReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    at: List[str] = Field(default_factory=list)
    quote: Optional[QuoteRef] = None


class ReplyAccepted(BaseModel):
    ok: bool
    message: str


class GroupMemberOut(BaseModel):
    """群成员候选（@ 用）。name 为群内显示名，与微信 @ 弹层一致。"""
    name: str = ""
    wxid: str = ""
    is_owner: bool = False


class GroupMembersOut(BaseModel):
    is_group: bool = False
    items: List[GroupMemberOut] = Field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# 分析日志
# ---------------------------------------------------------------------------
class AnalysisLogOut(BaseModel):
    id: int
    created_at: str = ""
    session_username: str = ""
    session_display: str = ""          # 来源会话显示名（备注/昵称优先，失败回退原值）
    sender_wxid: str = ""
    msg_count: int = 0
    input_text: str = ""
    input_text_display: str = ""       # 消息内容展示版（群消息发送者前缀已解析为显示名）
    result: str = ""                   # AI 结果原始 JSON（ok/below_threshold 时有值）
    status: str = ""                   # ok / error / skipped / below_threshold
    error: str = ""                    # skipped/error 原因
    model: str = ""
    latency_ms: Optional[int] = None   # AI 接口调用本身耗时
    agg_wait_ms: Optional[int] = None   # 收到消息 → 合并窗口 flush
    queue_wait_ms: Optional[int] = None  # 入队 → worker 取出
    e2e_ms: Optional[int] = None        # 收到消息 → 分析完成（端到端）


class AnalysisLogListOut(BaseModel):
    total: int
    items: List[AnalysisLogOut]

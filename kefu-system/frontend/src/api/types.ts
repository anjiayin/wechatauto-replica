/**
 * 与后端 app/schemas.py 字段一一对齐的 TS 类型（共享约定 8-1）。
 */

/** /api/status */
export interface WechatStatus {
  alive: boolean
  visible: boolean
}

export interface QueueStatus {
  size: number
  max: number
  dropped_total: number
}

export interface WorkerStatus {
  busy: number
  max: number
}

/** 监控对象配额（/api/status.monitor_quota） */
export interface MonitorQuota {
  used: number
  max: number
}

/** AI 分析健康度（/api/status.analysis_health） */
export interface AnalysisHealth {
  last_error: string
  consecutive_failures: number
}

/** /api/settings/monitor */
export interface MonitorSettings {
  max_targets: number
}

export interface StatusOut {
  listener_running: boolean
  monitored_count: number
  wechat: WechatStatus
  queue: QueueStatus
  worker: WorkerStatus
  monitor_quota: MonitorQuota
  analysis_health: AnalysisHealth
  ai_configured: boolean
  /** 是否启用 AI 接口（false = 轻量筛选模式，商机由关键词/正则产生） */
  ai_enabled: boolean
  watermark_saved_at: string
  degraded_reason: string
  unread_count: number
  /** 今日 AI 估算费用（元，反馈 13） */
  today_cost: number
  /** 各消息分片解密缓存健康度（可选，仅排查"消息停摆"类故障用） */
  cache?: CacheShardStatus[]
}

/** 消息分片解密缓存健康度（/api/status.cache） */
export interface CacheShardStatus {
  rel: string
  /** 距上次成功更新秒数；null = 从未成功 */
  stale_seconds: number | null
  last_ok_at: string
  last_error: string
  fail_streak: number
  rebuilding: boolean
}

/** /api/sessions/search */
export interface SessionItem {
  username: string
  display_name: string
  kind: 'group' | 'friend'
}

/** 消息（历史/上下文） */
export interface MessageItem {
  local_id: number
  type: string
  type_name: string
  sender_id: number
  sender_name: string
  create_time: string
  content: string
  sort_seq: number
}

/** /api/monitor-targets */
export interface MonitorTarget {
  id: number
  username: string
  display_name: string
  kind: 'group' | 'friend'
  enabled: number
}

/** /api/opportunity-types */
export interface OpportunityType {
  id: number
  name: string
  description: string
  enabled: number
  updated_at: string
}

/** /api/settings/ai */
export interface AISettings {
  base_url: string
  api_key_masked: string
  model: string
  timeout_sec: number
  temperature: number
  /** 关闭思考模式（思考型模型默认会花大量隐藏 token，响应慢且波动大） */
  disable_thinking: boolean
  /** 是否使用 AI 接口（关闭后改用「轻量筛选」关键词/正则判定商机） */
  enabled: boolean
}

export interface AISettingsPayload {
  base_url: string
  api_key: string
  model: string
  timeout_sec: number
  temperature: number
  disable_thinking: boolean
  enabled: boolean
  confirm_compliance: boolean
}

export interface AITestResult {
  ok: boolean
  message: string
  latency_ms: number
  model: string
}

/** /api/settings/analysis */
export interface AnalysisSettings {
  strictness: 'lenient' | 'balanced' | 'strict'
  min_confidence: number
  prescreen_enabled: boolean
  prescreen_keywords: string
  prescreen_regex: string
  /** 分析微窗口：发送人静默 N 秒即触发 AI 分析（5–600，默认 15） */
  merge_window_sec: number
  /** 商机合并窗口：同一人 N 秒内再次命中合并为一条（30–1800，默认 180） */
  opp_merge_window_sec: number
  /** 漏判修正：用户确认过的真实商机消息示例（最多 20 条） */
  correction_examples: string[]
}

export interface AnalysisSettingsPayload extends AnalysisSettings {}

/** /api/opportunities */
export interface OpportunityListItem {
  id: number
  session_username: string
  session_name: string
  sender_wxid: string
  sender_name: string
  window_end: string
  type_id: number | null
  type_name: string
  summary: string
  /** 关键原文摘录（列表页「消息原文」列） */
  key_quote: string
  confidence: number
  /** 买/卖货方向（反馈 4）：buy=买货 sell=卖货 ''=不明 */
  direction: '' | 'buy' | 'sell'
  status: 'pending' | 'replied' | 'ignored' | 'handled'
  created_at: string
  /** 未读标识（内存态；打开详情或点击行内「未读」后清除） */
  unread: boolean
}

export interface OpportunityListOut {
  total: number
  items: OpportunityListItem[]
}

export interface OpportunityDetail extends OpportunityListItem {
  window_start: string
  merged_count: number
  raw_text: string
  /** 原文展示版：群消息每行发送者 ID 已替换为群昵称（反馈 6） */
  raw_text_display: string
  key_quote: string
  reply_content: string | null
  replied_at: string | null
  // 上下文已拆分为独立接口 GET /api/opportunities/{id}/context
}

/** GET /api/opportunities/{id}/context（error 非空 = 微信库读取失败原因） */
export interface OpportunityContextOut {
  items: MessageItem[]
  error: string
}

/** /api/analysis-logs 单条分析日志 */
export interface AnalysisLogItem {
  id: number
  created_at: string
  session_username: string
  /** 来源会话显示名（备注/昵称优先，解析失败回退 session_username） */
  session_display: string
  sender_wxid: string
  msg_count: number
  input_text: string
  /** 消息内容展示版（群消息发送者前缀已解析为显示名） */
  input_text_display: string
  result: string
  status: 'ok' | 'error' | 'skipped' | 'below_threshold'
  error: string
  model: string
  /** AI 接口调用本身耗时 */
  latency_ms: number | null
  /** 收到消息 → 合并窗口 flush 的等待 */
  agg_wait_ms: number | null
  /** 入队 → worker 取出（排队） */
  queue_wait_ms: number | null
  /** 收到消息 → 分析完成（端到端） */
  e2e_ms: number | null
}

/** /api/analysis-logs 分页响应 */
export interface AnalysisLogListOut {
  total: number
  items: AnalysisLogItem[]
}

export interface ReplyPayload {
  text: string
  at: string[]
  quote?: { local_id: number }
}

/** /api/opportunities/{id}/members：@ 群成员候选（name 为群内显示名） */
export interface GroupMember {
  name: string
  wxid: string
  is_owner: boolean
}

export interface GroupMembers {
  is_group: boolean
  items: GroupMember[]
  error: string
}

/** /api/system/retention（反馈 7：历史数据保留配置，天） */
export interface RetentionSettings {
  logs_days: number
  opportunities_days: number
}

/** POST /api/system/purge 结果：dry_run=true 时 deleted 为「将删除条数」 */
export interface PurgeResult {
  table: string
  deleted: number
  dry_run: boolean
  /** SQLite 计算的截断点（YYYY-MM-DD HH:MM:SS），删除早于该时刻的记录 */
  cutoff: string
}

/** GET /api/system/purge/last：上次清理记录（无记录时后端返回 {}） */
export interface LastPurge {
  table?: string
  deleted?: number
  keep_days?: number
  cutoff?: string
  at?: string
}

/** /api/system/billing 单日用量与费用（元） */
export interface BillingDay {
  date: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cost: number
}

/** /api/system/billing（反馈 13：AI 账单估算） */
export interface BillingOut {
  unit: 'per_k' | 'per_m'
  input_price: number
  output_price: number
  today: BillingDay
  series: BillingDay[]
}

/** /api/system/cache（微信数据库解密缓存目录） */
export interface CacheDirInfo {
  dir: string
  default_dir: string
  configured: string
  is_default: boolean
  size_bytes: number
  file_count: number
  active_dir: string
  active: boolean
  legacy_dirs: string[]
}

/** WS 事件（{event, data}） */
export interface WsEvent {
  event: string
  data: unknown
}

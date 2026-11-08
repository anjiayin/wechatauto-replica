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
  watermark_saved_at: string
  degraded_reason: string
  unread_count: number
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
}

export interface AISettingsPayload {
  base_url: string
  api_key: string
  model: string
  timeout_sec: number
  temperature: number
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
  status: 'pending' | 'replied' | 'ignored' | 'handled'
  created_at: string
}

export interface OpportunityListOut {
  total: number
  items: OpportunityListItem[]
}

export interface OpportunityDetail extends OpportunityListItem {
  window_start: string
  merged_count: number
  raw_text: string
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
  latency_ms: number | null
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

/** WS 事件（{event, data}） */
export interface WsEvent {
  event: string
  data: unknown
}

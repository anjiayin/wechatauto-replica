import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  Paper,
  Pagination,
  Snackbar,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  Tab,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material'
import { apiDelete, apiGet, apiPost, apiPut, qs } from '../api/client'
import type {
  SessionItem,
  StatusOut,
  SummaryContent,
  SummaryContextOut,
  SummaryGenerateIn,
  SummaryMemberItem,
  SummaryMembersOut,
  SummaryOut,
  SummaryPromptTemplate,
  SummarySettings,
  SummaryStyle,
} from '../api/types'

const STYLE_OPTIONS: { value: SummaryStyle; label: string }[] = [
  { value: 'detailed', label: '全面' },
  { value: 'concise', label: '简洁' },
  { value: 'bullets', label: '要点式' },
]

const SECTIONS: { key: keyof SummaryContent; label: string }[] = [
  { key: 'reminders', label: '重要提醒' },
  { key: 'topics', label: '热门话题' },
  { key: 'comments', label: '点评' },
  { key: 'followups', label: '待跟进事项' },
  { key: 'others', label: '其他讨论' },
  { key: 'closing', label: '结语' },
]

const NO_PRICE_HINT = '请先在设置页配置模型单价，否则金额预算不生效'

/** 提示词模板上限（与后端 /settings 校验一致），仅用于前端禁用新增按钮 */
const MAX_TEMPLATES = 50

/** 安全解析 settings 里的提示词模板 JSON（损坏/结构不对一律回退空数组） */
function parseTemplates(raw: string | undefined): SummaryPromptTemplate[] {
  try {
    const parsed = JSON.parse(raw || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (t): t is SummaryPromptTemplate => Boolean(t) && typeof t.name === 'string',
    )
  } catch {
    return []
  }
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** Date → datetime-local 值（本地时区，无秒） */
function toLocalInput(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** datetime-local → 后端 TEXT 时间（T→空格、补 :00），绝不让 T 入 SQL */
function toBackendTime(value: string): string {
  const text = String(value || '').replace('T', ' ')
  return text.length === 16 ? `${text}:00` : text
}

function defaultRange(): { start: string; end: string } {
  const now = new Date()
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  return { start: toLocalInput(start), end: toLocalInput(now) }
}

/** 四处统一截断文案（结果区；txt/html/语料由后端生成同一主文案） */
function truncateLabel(row: SummaryOut): string {
  const base = `已截断：仅统计最近 ${row.msg_count} 条`
  return row.truncated_original > 0 ? `${base}（原共 ${row.truncated_original} 条）` : base
}

function parseContent(raw: string): SummaryContent {
  const empty: SummaryContent = {
    reminders: [], topics: [], comments: [], followups: [], others: [], closing: '',
  }
  try {
    const data = JSON.parse(raw || '{}') as Partial<SummaryContent>
    return {
      reminders: Array.isArray(data.reminders) ? data.reminders : [],
      topics: Array.isArray(data.topics) ? data.topics : [],
      comments: Array.isArray(data.comments) ? data.comments : [],
      followups: Array.isArray(data.followups) ? data.followups : [],
      others: Array.isArray(data.others) ? data.others : [],
      closing: typeof data.closing === 'string' ? data.closing : '',
    }
  } catch {
    return empty
  }
}

/** 总结正文（结果区与详情 Dialog 共用，无发送按钮）；onShowContext = P2 回链原文 */
function SummaryBody(
  { row, onShowContext }: { row: SummaryOut; onShowContext?: (r: SummaryOut) => void },
) {
  const content = useMemo(() => parseContent(row.content), [row.content])
  return (
    <Box>
      {row.truncated && (
        <Alert severity="warning" sx={{ mb: 2 }}>{truncateLabel(row)}</Alert>
      )}
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
        <Chip size="small" label={`生成时间：${row.created_at}`} />
        <Chip size="small" label={`耗时：${row.latency_ms ?? 0} ms`} />
        <Chip
          size="small"
          label={`实际 token：${row.prompt_tokens ?? '—'} + ${row.completion_tokens ?? '—'}`}
        />
      </Stack>
      <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap>
        <Typography variant="caption" color="text.secondary">
          参数快照：{row.session_name} · {row.time_start} ~ {row.time_end} ·
          消息 {row.msg_count} 条 / 成员 {row.member_count} 人 · 风格
          {STYLE_OPTIONS.find((s) => s.value === row.style)?.label ?? row.style}
          {row.custom_prompt ? ` · 补充要求：${row.custom_prompt}` : ''}
        </Typography>
        {onShowContext && (
          <Button size="small" variant="text" sx={{ ml: 'auto' }}
            onClick={() => onShowContext(row)}>
            查看原始消息
          </Button>
        )}
      </Stack>
      <Divider sx={{ my: 2 }} />
      {SECTIONS.map((section) => (
        <Box key={section.key} sx={{ mb: 2 }}>
          <Typography variant="h6" sx={{ fontSize: 16, fontWeight: 700, mb: 0.5 }}>
            {section.label}
          </Typography>
          {section.key === 'closing' ? (
            <Typography variant="body2">{content.closing || '（无）'}</Typography>
          ) : content[section.key].length > 0 ? (
            <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
              {(content[section.key] as string[]).map((line, i) => (
                <Typography component="li" variant="body2" key={i}>{line}</Typography>
              ))}
            </Box>
          ) : (
            <Typography variant="body2" color="text.secondary">（无）</Typography>
          )}
        </Box>
      ))}
    </Box>
  )
}

export default function GroupSummary() {
  const queryClient = useQueryClient()
  const [tab, setTab] = useState(0)
  const [session, setSession] = useState<SessionItem | null>(null)
  const [sessionKeyword, setSessionKeyword] = useState('')
  const [range, setRange] = useState(defaultRange)
  const [debouncedRange, setDebouncedRange] = useState(range)
  const [selectedWxids, setSelectedWxids] = useState<string[]>([])
  const [style, setStyle] = useState<SummaryStyle>('detailed')
  const [customPrompt, setCustomPrompt] = useState('')
  const [result, setResult] = useState<SummaryOut | null>(null)
  const [lastPayload, setLastPayload] = useState<SummaryGenerateIn | null>(null)
  const [snack, setSnack] = useState('')
  // P1-2 历史筛选：按群名（session_name/username 双列模糊）+ 日期
  const [historyKeyword, setHistoryKeyword] = useState('')
  const [historyDate, setHistoryDate] = useState('')
  const [historyPage, setHistoryPage] = useState(1)
  const [detail, setDetail] = useState<SummaryOut | null>(null)
  const [pendingDelete, setPendingDelete] = useState<SummaryOut | null>(null)
  // P1-3 提示词模板管理：管理弹窗开关 + 弹窗内新增表单草稿
  const [tplOpen, setTplOpen] = useState(false)
  const [tplName, setTplName] = useState('')
  const [tplPrompt, setTplPrompt] = useState('')
  // P2-1 回链原文：查看原始消息弹窗（loading 状态下 row 也非空，防误关提示）
  const [ctxRow, setCtxRow] = useState<SummaryOut | null>(null)
  const resultRef = useRef<HTMLDivElement>(null)

  // 群 / 时间变化防抖 500ms，避免每敲一个字符就查微信库
  useEffect(() => {
    const timer = window.setTimeout(
      () => setDebouncedRange({ ...range }), 500)
    return () => window.clearTimeout(timer)
  }, [range])

  // 设置页策略变化后统计行实时刷新（queryKey 带配置指纹）
  const { data: settings } = useQuery({
    queryKey: ['summary-settings'],
    queryFn: () => apiGet<SummarySettings>('/api/summary/settings'),
  })
  const settingsKey = settings ? JSON.stringify(settings) : ''
  // P1-3 提示词模板列表（settings 只读派生，写操作走 templatesMutation）
  const templates = useMemo(
    () => parseTemplates(settings?.['summary.prompt_templates']),
    [settings],
  )

  const { data: statusData } = useQuery({
    queryKey: ['status'],
    queryFn: () => apiGet<StatusOut>('/api/status'),
  })
  // 候选群来自当前监控微信库；切换账号后旧账号的群列表立即作废
  const accountKey = statusData?.wechat.account?.account ?? ''
  const { data: sessions = [] } = useQuery({
    queryKey: ['summary-sessions', accountKey, sessionKeyword],
    queryFn: () => apiGet<SessionItem[]>(
      `/api/sessions/search${qs({ kind: 'group', keyword: sessionKeyword, limit: 100 })}`),
  })

  const username = session?.username ?? ''
  const start = toBackendTime(debouncedRange.start)
  const end = toBackendTime(debouncedRange.end)
  const sendersParam = selectedWxids.join(',')
  const membersQuery = useQuery({
    queryKey: ['summary-members', username, start, end, sendersParam, settingsKey],
    queryFn: () => apiGet<SummaryMembersOut>(
      `/api/summary/members${qs({ session: username, start, end, senders: sendersParam })}`),
    enabled: Boolean(username && start && end),
  })
  const stats = membersQuery.data

  const generateMutation = useMutation({
    mutationFn: (payload: SummaryGenerateIn) =>
      apiPost<SummaryOut>('/api/summary/generate', payload),
    onSuccess: (data) => {
      setResult(data)
      window.setTimeout(() => resultRef.current?.scrollIntoView({ behavior: 'smooth' }), 80)
      void queryClient.invalidateQueries({ queryKey: ['summary-history'] })
    },
    onError: (error: Error) => setSnack(`生成失败：${error.message}`),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => apiDelete(`/api/summary/history/${id}`),
    onSuccess: () => {
      setPendingDelete(null)
      setDetail(null)
      if (result?.id === pendingDelete?.id) setResult(null)
      setSnack('总结已删除')
      void queryClient.invalidateQueries({ queryKey: ['summary-history'] })
    },
    onError: (error: Error) => setSnack(`删除失败：${error.message}`),
  })

  // P1-3 模板管理：settings 只传 summary.prompt_templates 一个键，
  // 后端 exclude_none 语义下其余键保持不变
  const templatesMutation = useMutation({
    mutationFn: (list: SummaryPromptTemplate[]) =>
      apiPut<SummarySettings>('/api/summary/settings', {
        'summary.prompt_templates': JSON.stringify(list),
      }),
    onSuccess: (_data, list) => {
      void queryClient.invalidateQueries({ queryKey: ['summary-settings'] })
      setSnack(list.length ? '提示词模板已保存' : '已恢复默认（模板已清空）')
      if (!list.length) setTplOpen(false)
    },
    onError: (error: Error) => setSnack(`模板保存失败：${error.message}`),
  })

  // P2-1 回链原文：打开弹窗即拉取时间范围内的原始消息（与总结同源取数口径）
  const ctxUsername = ctxRow?.session_username ?? ''
  const ctxStart = ctxRow?.time_start ?? ''
  const ctxEnd = ctxRow?.time_end ?? ''
  const { data: ctxData, isFetching: ctxFetching, error: ctxError } = useQuery({
    queryKey: ['summary-context', ctxUsername, ctxStart, ctxEnd],
    queryFn: () => apiGet<SummaryContextOut>(
      `/api/summary/context${qs({ session: ctxUsername, start: ctxStart, end: ctxEnd })}`),
    enabled: Boolean(ctxRow),
  })

  const { data: history, isFetching: historyFetching } = useQuery({
    queryKey: ['summary-history', historyKeyword, historyDate, historyPage],
    queryFn: () => apiGet<{ total: number; items: SummaryOut[] }>(
      `/api/summary/history${qs({
        page: historyPage, page_size: 10, keyword: historyKeyword, date: historyDate,
      })}`),
    enabled: tab === 1,
  })

  const blocked = Boolean(
    !username || !stats || stats.error || stats.msg_count === 0
    || (stats.over_budget && stats.action === 'block'),
  )
  const blockReason = !username ? '请先选择群聊'
    : stats?.error ? stats.error
      : !stats || stats.msg_count === 0 ? '所选时间范围/成员没有消息，无法生成'
        : stats.over_budget && stats.action === 'block' ? '预估已超预算且当前策略为阻止生成，请收窄范围或在设置页调整预算/策略'
          : ''

  const submit = () => {
    if (!username || blocked) return
    const payload: SummaryGenerateIn = {
      session: username,
      start,
      end,
      senders: selectedWxids,
      style,
      custom_prompt: customPrompt,
    }
    setLastPayload(payload)
    generateMutation.mutate(payload)
  }

  const memberItems = stats?.items ?? []
  const selectedMembers = memberItems.filter((m) => selectedWxids.includes(m.sender_wxid))

  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Tabs value={tab} onChange={(_, v: number) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="新建总结" />
        <Tab label="历史记录" />
      </Tabs>

      {tab === 0 && (
        <Stack spacing={2}>
          <Paper variant="outlined" sx={{ p: 2 }}>
            <Typography variant="subtitle2" sx={{ mb: 2 }}>总结条件</Typography>
            <Stack spacing={2}>
              <Autocomplete
                options={sessions}
                value={session}
                getOptionLabel={(o) => o.display_name || o.username}
                isOptionEqualToValue={(a, b) => a.username === b.username}
                onInputChange={(_, value) => setSessionKeyword(value.trim())}
                onChange={(_, value) => {
                  setSession(value)
                  setSelectedWxids([])
                }}
                renderOption={(props, option) => {
                  // 同名群区分：第二行灰字显示 username（唯一标识）
                  const key = props.key ?? option.username
                  return (
                    <Box component="li" {...props} key={key}
                      sx={{ display: 'block', py: 0.5 }}>
                      <Typography variant="body2">
                        {option.display_name || option.username}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {option.username}
                      </Typography>
                    </Box>
                  )
                }}
                renderInput={(params) => (
                  <TextField {...params} label="选择群聊" size="small"
                    placeholder="输入群名搜索" />
                )}
              />
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                <TextField
                  label="开始时间" type="datetime-local" size="small" fullWidth
                  value={range.start}
                  onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))}
                  InputLabelProps={{ shrink: true }}
                />
                <TextField
                  label="结束时间" type="datetime-local" size="small" fullWidth
                  value={range.end}
                  onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))}
                  InputLabelProps={{ shrink: true }}
                />
              </Stack>
              <Box>
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                  <Typography variant="caption" color="text.secondary">
                    成员筛选（不选 = 全部成员）
                  </Typography>
                  <Button size="small" onClick={() => setSelectedWxids(memberItems.map((m) => m.sender_wxid))}>
                    全选
                  </Button>
                  <Button size="small" onClick={() => setSelectedWxids([])}>清空</Button>
                </Stack>
                <Autocomplete
                  multiple
                  options={memberItems}
                  value={selectedMembers}
                  loading={membersQuery.isFetching}
                  getOptionLabel={(o) => `${o.display_name || o.sender_wxid}（${o.count} 条）`}
                  isOptionEqualToValue={(a, b) => a.sender_wxid === b.sender_wxid}
                  onChange={(_, values) => setSelectedWxids(values.map((v) => v.sender_wxid))}
                  renderInput={(params) => (
                    <TextField {...params} size="small" label="发言成员（可多选）"
                      placeholder="搜索成员名" />
                  )}
                />
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">总结风格</Typography>
                <ToggleButtonGroup
                  exclusive size="small" value={style} sx={{ display: 'flex', mt: 0.5 }}
                  onChange={(_, value: SummaryStyle | null) => {
                    if (value) setStyle(value)
                  }}
                >
                  {STYLE_OPTIONS.map((option) => (
                    <ToggleButton key={option.value} value={option.value}>
                      {option.label}
                    </ToggleButton>
                  ))}
                </ToggleButtonGroup>
              </Box>
              {/* P1-3 提示词模板：下拉快速选用（选中即填充下方补充要求），右侧入口管理模板 */}
              <Box>
                <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
                  <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>
                    提示词模板（选中即填充到下方自定义提示词）
                  </Typography>
                  {templates.length > 0 && (
                    <Button size="small" onClick={() => setCustomPrompt('')}>清空</Button>
                  )}
                  <Button size="small" onClick={() => {
                    setTplName(''); setTplPrompt('')
                    setTplOpen(true)
                  }}>
                    管理模板
                  </Button>
                </Stack>
                <Autocomplete
                  options={templates}
                  value={null}
                  blurOnSelect
                  getOptionLabel={(t) => t.name}
                  onChange={(_, t) => { if (t) setCustomPrompt(t.prompt) }}
                  renderOption={(props, option) => {
                    const key = props.key ?? option.name
                    return (
                      <Box component="li" {...props} key={key} sx={{ display: 'block', py: 0.5 }}>
                        <Typography variant="body2">{option.name}</Typography>
                        <Typography variant="caption" color="text.secondary"
                          sx={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {option.prompt}
                        </Typography>
                      </Box>
                    )
                  }}
                  renderInput={(params) => (
                    <TextField {...params} size="small" fullWidth
                      placeholder={templates.length ? '选择模板快速填充' : '暂无模板，点右上「管理模板」新建'}
                      helperText="模板长期保存、可复用；生成时仍以下方自定义提示词为准" />
                  )}
                />
              </Box>
              <TextField
                label="自定义提示词（可选）" multiline minRows={2} maxRows={5}
                size="small" fullWidth value={customPrompt}
                placeholder="例如：重点关注价格咨询和售后投诉"
                inputProps={{ maxLength: 500 }}
                onChange={(e) => setCustomPrompt(e.target.value)}
                helperText={`${customPrompt.length}/500 字`}
              />
            </Stack>
          </Paper>

          <Alert severity={blocked ? 'warning' : 'info'}>
            {blockReason || (
              <>
                {stats?.msg_count ?? 0} 条消息 /
                {selectedWxids.length ? selectedMembers.length : memberItems.length} 位成员，
                预估 token ≈ {(stats?.est_tokens ?? 0).toLocaleString()}，
                占预算 {((stats?.usage_ratio ?? 0) * 100).toFixed(1)}%
                （{stats?.budget_text || '—'}；当前策略：
                {stats?.action === 'block' ? '阻止生成' : `最近 ${stats?.truncate_n ?? 0} 条截断`}）
                {stats?.budget_hint && (
                  <Box component="div" sx={{ color: '#a3231d', fontWeight: 700, mt: 0.5 }}>
                    {stats.budget_hint}
                  </Box>
                )}
              </>
            )}
          </Alert>

          <Box>
            <Button variant="contained" onClick={submit} disabled={blocked || generateMutation.isPending}>
              {generateMutation.isPending ? '生成中…' : '生成总结'}
            </Button>
            {generateMutation.isPending && <CircularProgress size={20} sx={{ ml: 2 }} />}
            {generateMutation.isPending && (
              <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                AI 调用通常需要数秒至数十秒，请稍候
              </Typography>
            )}
          </Box>

          {result && (
            <Paper variant="outlined" sx={{ p: 2 }} ref={resultRef}>
              <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
                <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>总结结果</Typography>
                <Stack direction="row" spacing={1}>
                  <Button size="small" component="a"
                    href={`/api/summary/history/${result.id}/export?format=txt`}>
                    导出 txt
                  </Button>
                  <Button size="small" component="a"
                    href={`/api/summary/history/${result.id}/export?format=html`}>
                    导出 html
                  </Button>
                  <Button size="small" onClick={submit}
                    disabled={generateMutation.isPending}>重新生成</Button>
                </Stack>
              </Stack>
              <SummaryBody row={result} onShowContext={setCtxRow} />
            </Paper>
          )}
        </Stack>
      )}

      {tab === 1 && (
        <Box>
          <Stack direction="row" spacing={2} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
            <TextField label="按群名搜索" size="small" value={historyKeyword}
              onChange={(e) => { setHistoryKeyword(e.target.value); setHistoryPage(1) }}
              placeholder="群显示名或 username，模糊匹配"
              sx={{ minWidth: 220 }} />
            <TextField label="生成日期" type="date" size="small" value={historyDate}
              InputLabelProps={{ shrink: true }}
              onChange={(e) => { setHistoryDate(e.target.value); setHistoryPage(1) }} />
            {historyFetching && <CircularProgress size={20} />}
          </Stack>
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>生成时间</TableCell>
                  <TableCell>群名</TableCell>
                  <TableCell>时间范围</TableCell>
                  <TableCell>成员摘要</TableCell>
                  <TableCell>风格</TableCell>
                  <TableCell>token</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(history?.items ?? []).map((row) => (
                  <TableRow key={row.id} hover sx={{ cursor: 'pointer' }}
                    onClick={() => setDetail(row)}>
                    <TableCell>{row.created_at}</TableCell>
                    <TableCell>{row.session_name}</TableCell>
                    <TableCell sx={{ fontSize: 12 }}>
                      {row.time_start} ~ {row.time_end}
                    </TableCell>
                    <TableCell>{
                      (() => {
                        try {
                          const names = JSON.parse(row.member_names || '[]') as string[]
                          return names.length ? names.slice(0, 3).join('、') + (names.length > 3 ? '…' : '') : '全部'
                        } catch {
                          return '全部'
                        }
                      })()
                    }</TableCell>
                    <TableCell>
                      {STYLE_OPTIONS.find((s) => s.value === row.style)?.label ?? row.style}
                    </TableCell>
                    <TableCell>{(row.prompt_tokens ?? 0) + (row.completion_tokens ?? 0)}</TableCell>
                  </TableRow>
                ))}
                {!historyFetching && (history?.items.length ?? 0) === 0 && (
                  <TableRow>
                    <TableCell colSpan={6} align="center">暂无历史总结</TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>
          <Stack alignItems="center" sx={{ mt: 2 }}>
            <Pagination count={Math.max(1, Math.ceil((history?.total ?? 0) / 10))}
              page={historyPage} onChange={(_, page) => setHistoryPage(page)} />
          </Stack>
        </Box>
      )}

      {/* 历史详情（只读） */}
      <Dialog open={detail !== null} onClose={() => setDetail(null)} maxWidth="md" fullWidth>
        <DialogTitle>总结详情</DialogTitle>
        <DialogContent dividers>
          {detail && <SummaryBody row={detail} onShowContext={setCtxRow} />}
        </DialogContent>
        <DialogActions>
          {detail && (
            <>
              <Button component="a" size="small"
                href={`/api/summary/history/${detail.id}/export?format=txt`}>导出 txt</Button>
              <Button component="a" size="small"
                href={`/api/summary/history/${detail.id}/export?format=html`}>导出 html</Button>
              <Button color="error" size="small" onClick={() => setPendingDelete(detail)}>删除</Button>
            </>
          )}
          <Button onClick={() => setDetail(null)}>关闭</Button>
        </DialogActions>
      </Dialog>

      {/* 删除二次确认 */}
      <Dialog open={pendingDelete !== null} onClose={() => setPendingDelete(null)}>
        <DialogTitle>确认删除这条总结？</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {pendingDelete?.session_name} · {pendingDelete?.time_start} ~ {pendingDelete?.time_end}
            <br />删除后不可恢复。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPendingDelete(null)}>取消</Button>
          <Button color="error" onClick={() => pendingDelete && deleteMutation.mutate(pendingDelete.id)}>
            确认删除
          </Button>
        </DialogActions>
      </Dialog>

      {/* P1-3 提示词模板管理（增删 / 恢复默认=写回 []，恢复前二次确认） */}
      <Dialog open={tplOpen} onClose={() => setTplOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>提示词模板管理</DialogTitle>
        <DialogContent dividers>
          <Stack spacing={1.5} sx={{ mb: 2 }}>
            {templates.map((t) => (
              <Paper key={t.name} variant="outlined" sx={{ p: 1.5 }}>
                <Stack direction="row" alignItems="center" spacing={1}>
                  <Typography variant="body2" sx={{ fontWeight: 700, flex: 1, minWidth: 0 }}>
                    {t.name}
                  </Typography>
                  <Button
                    size="small"
                    onClick={() => setCustomPrompt(t.prompt)}
                  >
                    填充到表单
                  </Button>
                  <Button
                    size="small" color="error"
                    disabled={templatesMutation.isPending}
                    onClick={() => templatesMutation.mutate(
                      templates.filter((x) => x.name !== t.name),
                    )}
                  >
                    删除
                  </Button>
                </Stack>
                <Typography variant="caption" color="text.secondary"
                  sx={{ display: 'block', mt: 0.5, whiteSpace: 'pre-wrap' }}>
                  {t.prompt || '（空）'}
                </Typography>
              </Paper>
            ))}
            {templates.length === 0 && (
              <Typography variant="body2" color="text.secondary">
                暂无模板。常用提示词保存为模板后，可在新建总结时快速选用。
              </Typography>
            )}
          </Stack>
          <Divider sx={{ mb: 2 }} />
          <Stack spacing={1.5}>
            <Typography variant="subtitle2">新增模板</Typography>
            <TextField
              label="模板名称" size="small" fullWidth value={tplName}
              inputProps={{ maxLength: 50 }}
              onChange={(e) => setTplName(e.target.value)}
              placeholder="例如：售后投诉专项"
            />
            <TextField
              label="提示词内容" size="small" fullWidth multiline minRows={2} maxRows={6}
              value={tplPrompt} inputProps={{ maxLength: 500 }}
              onChange={(e) => setTplPrompt(e.target.value)}
              placeholder="例如：重点关注价格咨询和售后投诉，忽略闲聊"
              helperText={`${tplPrompt.length}/500 字`}
            />
            <Button
              size="small" variant="outlined" sx={{ alignSelf: 'flex-start' }}
              disabled={templatesMutation.isPending
                || !tplName.trim() || !tplPrompt.trim()
                || templates.length >= MAX_TEMPLATES}
              onClick={() => templatesMutation.mutate([
                ...templates,
                { name: tplName.trim(), prompt: tplPrompt },
              ])}
            >
              {templates.length >= MAX_TEMPLATES ? '模板数已达上限（50）' : '保存为新模板'}
            </Button>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button
            size="small" color="warning"
            disabled={templatesMutation.isPending || templates.length === 0}
            onClick={() => {
              if (window.confirm(
                `确定恢复默认并清空全部 ${templates.length} 个模板？此操作不可撤销。`,
              )) templatesMutation.mutate([])
            }}
          >
            恢复默认
          </Button>
          <Button onClick={() => setTplOpen(false)}>关闭</Button>
        </DialogActions>
      </Dialog>

      {/* P2-1 回链原文：按总结时间范围查看原始消息（与总结同源取数口径） */}
      <Dialog open={ctxRow !== null} onClose={() => setCtxRow(null)} maxWidth="md" fullWidth>
        <DialogTitle>
          原始消息（{ctxRow?.session_name || ctxRow?.session_username || ''}
          ，{ctxStart} ~ {ctxEnd}）
        </DialogTitle>
        <DialogContent dividers>
          {ctxFetching && (
            <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 4 }}>
              <CircularProgress size={20} />
              <Typography variant="body2" color="text.secondary">
                正在读取原始消息…
              </Typography>
            </Stack>
          )}
          {!ctxFetching && ctxError && (
            <Alert severity="error">
              读取失败：{(ctxError as Error).message}
            </Alert>
          )}
          {!ctxFetching && !ctxError && ctxData && ctxData.items.length === 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
              该时间范围内没有原始消息（可能已被微信数据库清理）。
            </Typography>
          )}
          {!ctxFetching && !ctxError && ctxData && ctxData.items.length > 0 && (
            <Stack spacing={1}>
              <Typography variant="caption" color="text.secondary">
                共 {ctxData.total} 条，按发送时间正序排列
              </Typography>
              {ctxData.items.map((m) => (
                <Paper key={`${m.local_id}-${m.create_time}`} variant="outlined" sx={{ p: 1 }}>
                  <Stack direction="row" spacing={1} alignItems="baseline">
                    <Typography variant="body2" sx={{ fontWeight: 700, flexShrink: 0 }}>
                      {m.sender_name || m.sender_wxid || '（未知成员）'}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {m.create_time}
                    </Typography>
                  </Stack>
                  <Typography variant="body2" sx={{ mt: 0.5, whiteSpace: 'pre-wrap' }}>
                    {m.content || '（非文本消息，原文不展示）'}
                  </Typography>
                </Paper>
              ))}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCtxRow(null)}>关闭</Button>
        </DialogActions>
      </Dialog>

      <Snackbar open={Boolean(snack)} autoHideDuration={6000}
        onClose={() => setSnack('')} message={snack}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
        action={lastPayload && snack.startsWith('生成失败') ? (
          <Button color="primary" size="small"
            onClick={() => { setSnack(''); generateMutation.mutate(lastPayload) }}>
            重试
          </Button>
        ) : undefined}
      />
    </Paper>
  )
}

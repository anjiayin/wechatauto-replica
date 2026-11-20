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
import { apiDelete, apiGet, apiPost, qs } from '../api/client'
import type {
  SessionItem,
  SummaryContent,
  SummaryGenerateIn,
  SummaryMemberItem,
  SummaryMembersOut,
  SummaryOut,
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

/** 总结正文（结果区与详情 Dialog 共用，无发送按钮） */
function SummaryBody({ row }: { row: SummaryOut }) {
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
      <Typography variant="caption" color="text.secondary">
        参数快照：{row.session_name} · {row.time_start} ~ {row.time_end} ·
        消息 {row.msg_count} 条 / 成员 {row.member_count} 人 · 风格
        {STYLE_OPTIONS.find((s) => s.value === row.style)?.label ?? row.style}
        {row.custom_prompt ? ` · 补充要求：${row.custom_prompt}` : ''}
      </Typography>
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
  const [historySession, setHistorySession] = useState('')
  const [historyDate, setHistoryDate] = useState('')
  const [historyPage, setHistoryPage] = useState(1)
  const [detail, setDetail] = useState<SummaryOut | null>(null)
  const [pendingDelete, setPendingDelete] = useState<SummaryOut | null>(null)
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

  const { data: sessions = [] } = useQuery({
    queryKey: ['summary-sessions', sessionKeyword],
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

  const { data: history, isFetching: historyFetching } = useQuery({
    queryKey: ['summary-history', historySession, historyDate, historyPage],
    queryFn: () => apiGet<{ total: number; items: SummaryOut[] }>(
      `/api/summary/history${qs({
        page: historyPage, page_size: 10, session: historySession, date: historyDate,
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
              <SummaryBody row={result} />
            </Paper>
          )}
        </Stack>
      )}

      {tab === 1 && (
        <Box>
          <Stack direction="row" spacing={2} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
            <TextField label="群名（username 精确）" size="small" value={historySession}
              onChange={(e) => { setHistorySession(e.target.value); setHistoryPage(1) }}
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
          {detail && <SummaryBody row={detail} />}
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

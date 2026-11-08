import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControlLabel,
  IconButton,
  InputAdornment,
  List,
  ListItem,
  ListItemText,
  MenuItem,
  Paper,
  Skeleton,
  Slider,
  Snackbar,
  Stack,
  Switch,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import SearchIcon from '@mui/icons-material/Search'
import AddIcon from '@mui/icons-material/Add'
import EditIcon from '@mui/icons-material/Edit'
import DeleteIcon from '@mui/icons-material/Delete'
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
  qs,
} from '../api/client'
import type {
  AISettings,
  AISettingsPayload,
  AITestResult,
  AnalysisSettings,
  MonitorSettings,
  MonitorTarget,
  OpportunityType,
  SessionItem,
  StatusOut,
} from '../api/types'
import {
  PRESCREEN_PRESETS,
  mergePrescreen,
  parsePrescreen,
} from '../constants/prescreen'

/** 设置页：监控对象 / 商机类型 / AI 接口（含分析判定与通知）三 tab。 */
export default function SettingsPage() {
  const [tab, setTab] = useState(0)
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Tabs value={tab} onChange={(_, v: number) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="监控对象" />
        <Tab label="商机类型" />
        <Tab label="AI 接口" />
      </Tabs>
      {tab === 0 && <TargetsTab />}
      {tab === 1 && <TypesTab />}
      {tab === 2 && <AiTab />}
    </Paper>
  )
}

// ---------------------------------------------------------------------------
// Tab 1：监控对象（通讯录双栏多选）
// ---------------------------------------------------------------------------
type SnackState = { msg: string; severity: 'success' | 'warning' | 'error' }

function TargetRow({
  target,
  checked,
  onToggleCheck,
  onToggleEnabled,
  onRemove,
}: {
  target: MonitorTarget
  checked: boolean
  onToggleCheck: () => void
  onToggleEnabled: () => void
  onRemove: () => void
}) {
  return (
    <ListItem
      dense
      secondaryAction={
        <Stack direction="row" spacing={0.5} alignItems="center">
          <Tooltip title={target.enabled === 1 ? '停用（暂停分析该会话）' : '启用'}>
            <Switch
              checked={target.enabled === 1}
              onChange={onToggleEnabled}
              size="small"
            />
          </Tooltip>
          <IconButton size="small" onClick={onRemove}>
            <DeleteIcon fontSize="small" />
          </IconButton>
        </Stack>
      }
    >
      <Checkbox
        size="small"
        checked={checked}
        onChange={onToggleCheck}
        sx={{ p: 0.5, mr: 1 }}
      />
      <ListItemText
        primary={target.display_name || target.username}
        secondary={`${target.kind === 'group' ? '群聊' : '私聊'} · ${target.username}${
          target.enabled === 1 ? '' : '（已停用）'
        }`}
      />
    </ListItem>
  )
}

function TargetsTab() {
  const queryClient = useQueryClient()
  // 左栏：通讯录候选（群聊/私聊子页签 + 搜索）
  const [candidateKind, setCandidateKind] = useState<'group' | 'friend'>('group')
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [checkedCandidates, setCheckedCandidates] = useState<Set<string>>(
    () => new Set(),
  )
  // 右栏：已选监控（可勾选批量移除）
  const [checkedTargets, setCheckedTargets] = useState<Set<number>>(
    () => new Set(),
  )
  const [snack, setSnack] = useState<SnackState | null>(null)

  // 搜索关键词 300ms 防抖（输入即过滤，不打爆接口）
  useEffect(() => {
    const t = window.setTimeout(() => setKeyword(keywordInput.trim()), 300)
    return () => window.clearTimeout(t)
  }, [keywordInput])

  const { data: statusData } = useQuery({
    queryKey: ['status'],
    queryFn: () => apiGet<StatusOut>('/api/status'),
  })
  const quota = statusData?.monitor_quota
  const quotaRatio = quota && quota.max > 0 ? quota.used / quota.max : 0
  const quotaWarning = quotaRatio >= 0.9
  const quotaFull = quota ? quota.used >= quota.max : false

  // 监控上限配置（monitor.max_targets，10~2000；保存后配额提示即时更新）
  const { data: monitorSettings } = useQuery({
    queryKey: ['monitor-settings'],
    queryFn: () => apiGet<MonitorSettings>('/api/settings/monitor'),
  })
  const [maxTargetsInput, setMaxTargetsInput] = useState('')
  const [maxTargetsLoaded, setMaxTargetsLoaded] = useState(false)
  if (monitorSettings && !maxTargetsLoaded) {
    setMaxTargetsInput(String(monitorSettings.max_targets))
    setMaxTargetsLoaded(true)
  }
  const maxTargetsValue = Number(maxTargetsInput)
  const maxTargetsValid =
    Number.isInteger(maxTargetsValue) &&
    maxTargetsValue >= 10 &&
    maxTargetsValue <= 2000
  const saveMaxTargetsMutation = useMutation({
    mutationFn: () =>
      apiPut<MonitorSettings>('/api/settings/monitor', {
        max_targets: maxTargetsValue,
      }),
    onSuccess: () => {
      setSnack({ msg: '监控上限已保存', severity: 'success' })
      void queryClient.invalidateQueries({ queryKey: ['monitor-settings'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
    onError: (err: Error) => setSnack({ msg: err.message, severity: 'error' }),
  })

  const { data: targets } = useQuery({
    queryKey: ['monitor-targets'],
    queryFn: () => apiGet<MonitorTarget[]>('/api/monitor-targets'),
  })

  // 群聊/私聊两个列表并行请求（Promise.all）：一次请求双端缓存，
  // 子页签切换零等待；后端另有 60s 联系人缓存，重复搜索毫秒级返回。
  const { data: candidateLists, isFetching: searching } = useQuery({
    queryKey: ['session-candidates', keyword],
    queryFn: async () => {
      const [groups, friends] = await Promise.all([
        apiGet<SessionItem[]>(
          `/api/sessions/search${qs({ kind: 'group', keyword, limit: 50 })}`,
        ),
        apiGet<SessionItem[]>(
          `/api/sessions/search${qs({ kind: 'friend', keyword, limit: 50 })}`,
        ),
      ])
      return { group: groups, friend: friends }
    },
  })
  const candidates =
    candidateKind === 'group' ? (candidateLists?.group ?? []) : (candidateLists?.friend ?? [])

  const targetUsernames = new Set((targets ?? []).map((t) => t.username))

  const toggleCandidate = (username: string) => {
    setCheckedCandidates((prev) => {
      const next = new Set(prev)
      if (next.has(username)) next.delete(username)
      else next.add(username)
      return next
    })
  }

  const toggleTarget = (id: number) => {
    setCheckedTargets((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // 批量添加：逐个 POST（携带候选显示名快照，后端免二次查询，响应更快）；
  // 后端超限 400 的中文 detail 原样进 Snackbar
  const addMutation = useMutation({
    mutationFn: async (usernames: string[]) => {
      const nameMap = new Map(
        (candidates ?? []).map((s) => [s.username, s.display_name] as const),
      )
      const errors: string[] = []
      for (const u of usernames) {
        try {
          await apiPost<MonitorTarget>('/api/monitor-targets', {
            username: u,
            display_name: nameMap.get(u) ?? '',
          })
        } catch (err) {
          errors.push(`${u}：${(err as Error).message}`)
        }
      }
      return errors
    },
    onSuccess: (errors) => {
      if (errors.length > 0) {
        setSnack({ msg: errors.join('；'), severity: 'error' })
      } else {
        setSnack({
          msg: `已添加 ${checkedCandidates.size} 个监控对象`,
          severity: 'success',
        })
      }
      setCheckedCandidates(new Set())
      void queryClient.invalidateQueries({ queryKey: ['monitor-targets'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
      void queryClient.invalidateQueries({ queryKey: ['session-candidates'] })
    },
  })

  // 批量移除：逐个 DELETE
  const removeMutation = useMutation({
    mutationFn: async (ids: number[]) => {
      const errors: string[] = []
      for (const id of ids) {
        try {
          await apiDelete(`/api/monitor-targets/${id}`)
        } catch (err) {
          errors.push(`#${id}：${(err as Error).message}`)
        }
      }
      return errors
    },
    onSuccess: (errors) => {
      if (errors.length > 0) {
        setSnack({ msg: errors.join('；'), severity: 'error' })
      } else {
        setSnack({ msg: '已移除所选监控对象', severity: 'success' })
      }
      setCheckedTargets(new Set())
      void queryClient.invalidateQueries({ queryKey: ['monitor-targets'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
      void queryClient.invalidateQueries({ queryKey: ['session-candidates'] })
    },
  })

  const toggleEnabledMutation = useMutation({
    mutationFn: (t: MonitorTarget) =>
      apiPatch<MonitorTarget>(`/api/monitor-targets/${t.id}`, {
        enabled: t.enabled === 0,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['monitor-targets'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
    onError: (err: Error) => setSnack({ msg: err.message, severity: 'error' }),
  })

  const groupTargets = (targets ?? []).filter((t) => t.kind === 'group')
  const friendTargets = (targets ?? []).filter((t) => t.kind === 'friend')

  const renderTargetGroup = (title: string, items: MonitorTarget[]) => (
    <>
      <Typography variant="caption" color="text.secondary" sx={{ pl: 1 }}>
        {title}（{items.length}）
      </Typography>
      {items.map((t) => (
        <TargetRow
          key={t.id}
          target={t}
          checked={checkedTargets.has(t.id)}
          onToggleCheck={() => toggleTarget(t.id)}
          onToggleEnabled={() => toggleEnabledMutation.mutate(t)}
          onRemove={() => removeMutation.mutate([t.id])}
        />
      ))}
      {items.length === 0 && (
        <Typography variant="body2" color="text.secondary" sx={{ pl: 2, py: 1 }}>
          暂无
        </Typography>
      )}
    </>
  )

  return (
    <Box>
      {/* 配额提示：接近上限（≥90%）用警告色；右侧为监控上限配置项 */}
      <Stack
        direction="row"
        spacing={1.5}
        alignItems="center"
        sx={{ mb: 1.5, flexWrap: 'wrap' }}
        useFlexGap
      >
        {quota && (
          <Alert
            severity={quotaWarning ? 'warning' : 'info'}
            variant="outlined"
            sx={{ flex: 1, minWidth: 260 }}
          >
            已监控 {quota.used}/{quota.max}，还可添加 {quota.max - quota.used} 个
            {quotaWarning && '（已接近上限，建议清理不再需要的监控对象）'}
          </Alert>
        )}
        <TextField
          size="small"
          type="number"
          label="监控上限"
          sx={{ width: 120 }}
          value={maxTargetsInput}
          onChange={(e) => setMaxTargetsInput(e.target.value)}
          inputProps={{ min: 10, max: 2000 }}
        />
        <Button
          size="small"
          variant="outlined"
          disabled={!maxTargetsValid || saveMaxTargetsMutation.isPending}
          onClick={() => saveMaxTargetsMutation.mutate()}
        >
          保存上限
        </Button>
      </Stack>

      <Stack direction="row" spacing={2} alignItems="stretch">
        {/* 左栏：通讯录候选 */}
        <Box
          sx={{
            flex: 1,
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 1,
            p: 1.5,
            display: 'flex',
            flexDirection: 'column',
            minWidth: 0,
          }}
        >
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="subtitle2">通讯录候选</Typography>
            <Stack
              direction="row"
              spacing={0.5}
              sx={{ ml: 'auto' }}
              alignItems="center"
            >
              <Button
                size="small"
                variant={candidateKind === 'group' ? 'contained' : 'text'}
                onClick={() => setCandidateKind('group')}
              >
                群聊
              </Button>
              <Button
                size="small"
                variant={candidateKind === 'friend' ? 'contained' : 'text'}
                onClick={() => setCandidateKind('friend')}
              >
                私聊
              </Button>
            </Stack>
          </Stack>
          <TextField
            size="small"
            label="按昵称/群名搜索"
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            fullWidth
            sx={{ mb: 1 }}
            InputProps={{
              endAdornment: (
                <InputAdornment position="end">
                  {searching ? (
                    <SearchIcon fontSize="small" color="primary" />
                  ) : (
                    <SearchIcon fontSize="small" />
                  )}
                </InputAdornment>
              ),
            }}
          />
          <List
            dense
            sx={{
              flex: 1,
              minHeight: 220,
              maxHeight: 340,
              overflow: 'auto',
            }}
          >
            {/* 首次加载骨架屏（替代空白） */}
            {searching && candidates.length === 0 &&
              [0, 1, 2, 3, 4, 5].map((i) => (
                <ListItem key={i} dense>
                  <ListItemText
                    primary={<Skeleton variant="text" width="60%" height={22} />}
                    secondary={<Skeleton variant="text" width="40%" height={16} />}
                  />
                </ListItem>
              ))}
            {candidates.map((s) => {
              const monitored = targetUsernames.has(s.username)
              return (
                <ListItem
                  key={s.username}
                  dense
                  sx={monitored ? { opacity: 0.45 } : undefined}
                  secondaryAction={
                    monitored ? (
                      <Typography variant="caption" color="text.secondary">
                        已监控
                      </Typography>
                    ) : undefined
                  }
                >
                  <Checkbox
                    size="small"
                    disabled={monitored}
                    checked={checkedCandidates.has(s.username)}
                    onChange={() => toggleCandidate(s.username)}
                    sx={{ p: 0.5, mr: 1 }}
                  />
                  <ListItemText
                    primary={s.display_name}
                    secondary={s.username}
                  />
                </ListItem>
              )
            })}
            {candidates.length === 0 && !searching && (
              <ListItem dense>
                <ListItemText
                  primary="未找到匹配的会话"
                />
              </ListItem>
            )}
          </List>
          <Button
            variant="contained"
            startIcon={
              addMutation.isPending ? (
                <CircularProgress size={16} color="inherit" />
              ) : (
                <AddIcon />
              )
            }
            disabled={
              checkedCandidates.size === 0 || addMutation.isPending || quotaFull
            }
            onClick={() => addMutation.mutate([...checkedCandidates])}
            sx={{ mt: 1 }}
          >
            {addMutation.isPending
              ? '添加中…'
              : `添加 →${checkedCandidates.size > 0 ? `（${checkedCandidates.size}）` : ''}`}
          </Button>
        </Box>

        {/* 右栏：已选监控 */}
        <Box
          sx={{
            flex: 1,
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 1,
            p: 1.5,
            display: 'flex',
            flexDirection: 'column',
            minWidth: 0,
          }}
        >
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            已选监控（{targets?.length ?? 0}）
          </Typography>
          <List dense sx={{ flex: 1, minHeight: 220, maxHeight: 340, overflow: 'auto' }}>
            {renderTargetGroup('群聊', groupTargets)}
            {renderTargetGroup('私聊', friendTargets)}
            {(targets ?? []).length === 0 && (
              <ListItem dense>
                <ListItemText
                  primary="暂无监控对象"
                  secondary="从左侧通讯录勾选群/好友后批量添加，系统将实时分析其新消息"
                />
              </ListItem>
            )}
          </List>
          <Button
            variant="outlined"
            color="error"
            disabled={
              checkedTargets.size === 0 || removeMutation.isPending
            }
            onClick={() => removeMutation.mutate([...checkedTargets])}
            sx={{ mt: 1 }}
          >
            ← 移除{checkedTargets.size > 0 ? `（${checkedTargets.size}）` : ''}
          </Button>
        </Box>
      </Stack>

      <Snackbar
        open={snack !== null}
        autoHideDuration={4500}
        onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert
          severity={snack?.severity ?? 'info'}
          onClose={() => setSnack(null)}
          sx={{ width: '100%' }}
        >
          {snack?.msg}
        </Alert>
      </Snackbar>
    </Box>
  )
}

// ---------------------------------------------------------------------------
// Tab 2：商机类型
// ---------------------------------------------------------------------------
const EMPTY_TYPE_FORM = { id: 0, name: '', description: '', enabled: true }

function TypesTab() {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [form, setForm] = useState(EMPTY_TYPE_FORM)

  const { data: types } = useQuery({
    queryKey: ['opportunity-types'],
    queryFn: () => apiGet<OpportunityType[]>('/api/opportunity-types'),
  })

  const saveMutation = useMutation({
    mutationFn: () => {
      if (form.id) {
        return apiPatch<OpportunityType>(`/api/opportunity-types/${form.id}`, {
          name: form.name,
          description: form.description,
          enabled: form.enabled,
        })
      }
      return apiPost<OpportunityType>('/api/opportunity-types', {
        name: form.name,
        description: form.description,
        enabled: form.enabled,
      })
    },
    onSuccess: () => {
      setDialogOpen(false)
      void queryClient.invalidateQueries({ queryKey: ['opportunity-types'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const toggleMutation = useMutation({
    mutationFn: (t: OpportunityType) =>
      apiPatch<OpportunityType>(`/api/opportunity-types/${t.id}`, {
        enabled: t.enabled === 0,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['opportunity-types'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (t: OpportunityType) => apiDelete(`/api/opportunity-types/${t.id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['opportunity-types'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  return (
    <Box>
      {error && (
        <Alert severity="error" sx={{ mb: 1 }} onClose={() => setError('')}>
          {error}
        </Alert>
      )}
      <Stack
        direction="row"
        justifyContent="space-between"
        alignItems="center"
        spacing={2}
        sx={{ mb: 1 }}
      >
        <Typography
          variant="body2"
          color="text.secondary"
          sx={{ flex: 1, minWidth: 0 }}
        >
          用自然语言描述你的商机口径，描述修改后立即对后续消息生效。
          示例：「寻找机械加工订单的采购需求——客户明确提到需要 CNC/车铣/钣金加工、数量、交期等」
        </Typography>
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          sx={{ flexShrink: 0, whiteSpace: 'nowrap' }}
          onClick={() => {
            setForm(EMPTY_TYPE_FORM)
            setDialogOpen(true)
          }}
        >
          新增类型
        </Button>
      </Stack>
      <List dense>
        {(types ?? []).map((t) => (
          <ListItem
            key={t.id}
            sx={{ pr: 17 }} // 给右侧「开关 + 编辑 + 删除」按钮留固定空间，避免描述文字被覆盖
            secondaryAction={
              <Stack direction="row" spacing={1} alignItems="center">
                <Switch
                  checked={t.enabled === 1}
                  onChange={() => toggleMutation.mutate(t)}
                  size="small"
                />
                <IconButton
                  size="small"
                  onClick={() => {
                    setForm({
                      id: t.id,
                      name: t.name,
                      description: t.description,
                      enabled: t.enabled === 1,
                    })
                    setDialogOpen(true)
                  }}
                >
                  <EditIcon fontSize="small" />
                </IconButton>
                <IconButton size="small" onClick={() => deleteMutation.mutate(t)}>
                  <DeleteIcon fontSize="small" />
                </IconButton>
              </Stack>
            }
          >
            <ListItemText
              primary={`${t.name}${t.enabled === 1 ? '' : '（已停用）'}`}
              secondary={t.description}
            />
          </ListItem>
        ))}
        {(types ?? []).length === 0 && (
          <ListItem>
            <ListItemText primary="暂无商机类型" secondary="未配置类型时 AI 一律判定为不命中" />
          </ListItem>
        )}
      </List>

      <Dialog open={dialogOpen} onClose={() => setDialogOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>{form.id ? '编辑商机类型' : '新增商机类型'}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="类型名称"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <TextField
              label="自然语言描述（判定口径）"
              multiline
              minRows={3}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
            <Stack direction="row" alignItems="center">
              <Switch
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              />
              <Typography variant="body2">启用（停用后不参与判定）</Typography>
            </Stack>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>取消</Button>
          <Button
            variant="contained"
            disabled={!form.name.trim() || !form.description.trim() || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            保存
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}

// ---------------------------------------------------------------------------
// Tab 3：AI 接口 + 分析判定 + 通知
// ---------------------------------------------------------------------------
const STRICTNESS_OPTIONS = [
  { value: 'lenient', label: '宽松（宁多勿漏）' },
  { value: 'balanced', label: '均衡（推荐）' },
  { value: 'strict', label: '严格（宁缺勿滥）' },
]

function AiTab() {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const [testResult, setTestResult] = useState<AITestResult | null>(null)
  // AI 表单
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [timeoutSec, setTimeoutSec] = useState(60)
  const [temperature, setTemperature] = useState(0.2)
  const [compliance, setCompliance] = useState(false)
  const [loaded, setLoaded] = useState(false)

  const { data: ai } = useQuery({
    queryKey: ['ai-settings'],
    queryFn: () => apiGet<AISettings>('/api/settings/ai'),
  })
  // 首次加载填充表单（Key 只回掩码，留空表示不变更）
  if (ai && !loaded) {
    setBaseUrl(ai.base_url)
    setModel(ai.model)
    setTimeoutSec(ai.timeout_sec)
    setTemperature(ai.temperature)
    setLoaded(true)
  }

  const { data: analysis } = useQuery({
    queryKey: ['analysis-settings'],
    queryFn: () => apiGet<AnalysisSettings>('/api/settings/analysis'),
  })
  const [strictness, setStrictness] = useState('balanced')
  const [minConfidence, setMinConfidence] = useState(60)
  const [prescreenEnabled, setPrescreenEnabled] = useState(false)
  const [keywords, setKeywords] = useState('')
  // 预筛正则 = 勾选的内置预设 + 自定义正则（保存时以 | 合并，存储格式不变）
  const [selectedPresets, setSelectedPresets] = useState<string[]>([])
  const [customRegex, setCustomRegex] = useState('')
  const [mergeWindow, setMergeWindow] = useState(15)
  const [oppMergeWindow, setOppMergeWindow] = useState(180)
  const [analysisLoaded, setAnalysisLoaded] = useState(false)
  if (analysis && !analysisLoaded) {
    setStrictness(analysis.strictness)
    setMinConfidence(analysis.min_confidence)
    setPrescreenEnabled(analysis.prescreen_enabled)
    setKeywords(analysis.prescreen_keywords)
    const parsed = parsePrescreen(analysis.prescreen_regex)
    setSelectedPresets(parsed.selectedPresets)
    setCustomRegex(parsed.customRegex)
    setMergeWindow(analysis.merge_window_sec)
    setOppMergeWindow(analysis.opp_merge_window_sec)
    setAnalysisLoaded(true)
  }

  const togglePreset = (regex: string) => {
    setSelectedPresets((prev) =>
      prev.includes(regex) ? prev.filter((r) => r !== regex) : [...prev, regex],
    )
  }

  const saveAiMutation = useMutation({
    mutationFn: (payload: AISettingsPayload) =>
      apiPut<AISettings>('/api/settings/ai', payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['ai-settings'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
      setApiKey('')
      setError('')
    },
    onError: (err: Error) => setError(err.message),
  })

  const testMutation = useMutation({
    mutationFn: () => apiPost<AITestResult>('/api/settings/ai/test'),
    onSuccess: (r) => setTestResult(r),
    onError: (err: Error) => {
      setTestResult({ ok: false, message: err.message, latency_ms: 0, model: '' })
    },
  })

  const saveAnalysisMutation = useMutation({
    mutationFn: () =>
      apiPut<AnalysisSettings>('/api/settings/analysis', {
        strictness,
        min_confidence: minConfidence,
        prescreen_enabled: prescreenEnabled,
        prescreen_keywords: keywords,
        prescreen_regex: mergePrescreen(selectedPresets, customRegex),
        merge_window_sec: mergeWindow,
        opp_merge_window_sec: oppMergeWindow,
        correction_examples: analysis?.correction_examples ?? [],
      }),
    onSuccess: () => {
      setError('')
      void queryClient.invalidateQueries({ queryKey: ['analysis-settings'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const soundEnabled = window.localStorage.getItem('kefu.sound_enabled') === '1'
  const saveNotifyMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      apiPut<{ ok: boolean }>('/api/settings/notify', { sound_enabled: enabled }),
    onSuccess: (_resp, enabled) => {
      window.localStorage.setItem('kefu.sound_enabled', enabled ? '1' : '0')
      // 同页 storage 事件不触发：派发自定义事件让 App 实时感知开关变化
      window.dispatchEvent(
        new CustomEvent('kefu-sound-settings-changed', {
          detail: { enabled },
        }),
      )
    },
    onError: (err: Error) => setError(err.message),
  })

  // 提示音音量（0~100，默认 60；localStorage 与提示音开关同级）
  // 注意：键不存在时 getItem 返回 null 而 Number(null)===0——必须先判
  // null 再转数字，否则存量用户（从未设置过音量）会被静音
  const [volume, setVolume] = useState(() => {
    const raw = window.localStorage.getItem('kefu.sound_volume')
    if (raw === null || raw.trim() === '') return 60
    const n = Number(raw)
    return Number.isFinite(n) ? Math.min(100, Math.max(0, Math.round(n))) : 60
  })
  const changeVolume = (v: number) => {
    setVolume(v)
    window.localStorage.setItem('kefu.sound_volume', String(v))
    window.dispatchEvent(new CustomEvent('kefu-sound-settings-changed'))
  }

  // 系统通知授权状态（granted / default / denied / unsupported）
  const [notifPermission, setNotifPermission] = useState<string>(() =>
    typeof Notification === 'undefined' ? 'unsupported' : Notification.permission,
  )
  // 应用级系统通知开关（默认关；localStorage 与提示音开关同级）
  const [systemNotifyEnabled, setSystemNotifyEnabled] = useState(() => {
    try {
      return window.localStorage.getItem('kefu.system_notify_enabled') === '1'
    } catch {
      return false
    }
  })
  const toggleSystemNotify = (enabled: boolean) => {
    setSystemNotifyEnabled(enabled)
    try {
      window.localStorage.setItem(
        'kefu.system_notify_enabled',
        enabled ? '1' : '0',
      )
    } catch {
      // localStorage 不可用（隐私模式等）：仅内存态生效
    }
    // 打开开关且尚未授权时，引导用户完成浏览器授权
    if (enabled && notifPermission === 'default') {
      void requestNotification()
    }
  }
  const requestNotification = async () => {
    if (typeof Notification === 'undefined') return
    try {
      const result = await Notification.requestPermission()
      setNotifPermission(result)
      if (result === 'granted') {
        // 授权成功后立即弹一条测试通知，用户可直观确认效果
        try {
          const n = new Notification('系统通知已开启', {
            body: '页面不在前台时收到新商机，将通过系统通知提醒',
          })
          n.onclick = () => {
            window.focus()
            n.close()
          }
        } catch {
          // 测试通知失败不影响授权结果
        }
      }
    } catch {
      // 部分浏览器在非安全上下文拒绝授权请求：状态保持不变
    }
  }
  const notifPermissionLabel =
    notifPermission === 'granted'
      ? '已授权'
      : notifPermission === 'denied'
        ? '已被浏览器拒绝，需在浏览器站点设置中恢复授权'
        : notifPermission === 'unsupported'
          ? '当前浏览器不支持系统通知'
          : '未授权（打开开关后可授权）'
  // 授权按钮显示时机：
  // ① 开关打开且未授权（default）→ 引导授权；
  // ② 已授权但开关关闭 → 保持可再次授权状态
  const showAuthButton =
    (systemNotifyEnabled && notifPermission === 'default') ||
    (notifPermission === 'granted' && !systemNotifyEnabled)

  return (
    <Box sx={{ maxWidth: 640 }}>
      {error && (
        <Alert severity="error" sx={{ mb: 1 }} onClose={() => setError('')}>
          {error}
        </Alert>
      )}

      <Typography variant="subtitle2" sx={{ mb: 1 }}>AI 接口（OpenAI 兼容）</Typography>
      <Stack spacing={2} sx={{ mb: 2 }}>
        <TextField
          label="API 地址（base_url）"
          placeholder="如 https://api.deepseek.com/v1"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
        />
        <TextField
          label="API Key"
          type="password"
          placeholder={
            ai && ai.api_key_masked
              ? `已保存（${ai.api_key_masked}），留空表示不变更`
              : 'sk-...'
          }
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <TextField
          label="模型名"
          placeholder="如 deepseek-chat"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        />
        <Stack direction="row" spacing={2}>
          <TextField
            label="超时（秒）"
            type="number"
            sx={{ width: 140 }}
            value={timeoutSec}
            onChange={(e) => setTimeoutSec(Number(e.target.value) || 60)}
          />
          <TextField
            label="温度（建议 0.2）"
            type="number"
            sx={{ width: 160 }}
            inputProps={{ step: 0.1, min: 0, max: 2 }}
            value={temperature}
            onChange={(e) => setTemperature(Number(e.target.value) || 0.2)}
          />
        </Stack>
        <FormControlLabel
          control={
            <Switch
              checked={compliance}
              onChange={(e) => setCompliance(e.target.checked)}
            />
          }
          label="我已知晓：被监控会话的消息内容将被发送至我自备的第三方 AI 接口进行分析（首次保存需勾选）"
        />
        <Stack direction="row" spacing={1}>
          <Button
            variant="contained"
            disabled={!baseUrl.trim() || !model.trim() || saveAiMutation.isPending}
            onClick={() =>
              saveAiMutation.mutate({
                base_url: baseUrl.trim(),
                api_key: apiKey.trim(),
                model: model.trim(),
                timeout_sec: timeoutSec,
                temperature,
                confirm_compliance: compliance,
              })
            }
          >
            保存 AI 设置
          </Button>
          <Button
            variant="outlined"
            disabled={testMutation.isPending}
            onClick={() => testMutation.mutate()}
          >
            {testMutation.isPending ? '测试中…' : '测试连接'}
          </Button>
        </Stack>
        {testResult && (
          <Alert severity={testResult.ok ? 'success' : 'error'}>
            {testResult.message}
            {testResult.latency_ms > 0 && `（耗时 ${testResult.latency_ms} ms）`}
          </Alert>
        )}
      </Stack>

      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle2" sx={{ mb: 1 }}>分析判定</Typography>
      <Stack spacing={2} sx={{ mb: 2 }}>
        <TextField
          select
          label="判定严格度"
          value={strictness}
          onChange={(e) => setStrictness(e.target.value)}
          sx={{ width: 260 }}
        >
          {STRICTNESS_OPTIONS.map((o) => (
            <MenuItem key={o.value} value={o.value}>
              {o.label}
            </MenuItem>
          ))}
        </TextField>
        <Box>
          <Typography variant="body2" gutterBottom>
            最低置信度门槛：{minConfidence}（低于该值不生成商机）
          </Typography>
          <Slider
            value={minConfidence}
            min={0}
            max={100}
            step={5}
            marks
            valueLabelDisplay="auto"
            onChange={(_, v) => setMinConfidence(v as number)}
            sx={{ width: 320 }}
          />
        </Box>
        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={prescreenEnabled}
                onChange={(e) => setPrescreenEnabled(e.target.checked)}
              />
            }
            label="启用轻量预筛（关键词/正则未命中不调用 AI，节省成本）"
          />
          {prescreenEnabled && (
            <Stack spacing={1} sx={{ mt: 1 }}>
              <TextField
                label="预筛关键词"
                helperText="多个关键词用逗号（中英文均可）分隔"
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                sx={{ width: 420 }}
              />
              <Box>
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={{ display: 'block', mb: 0.5 }}
                >
                  内置预设（可多选）
                </Typography>
                <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }} useFlexGap>
                  {PRESCREEN_PRESETS.map((p) => {
                    const selected = selectedPresets.includes(p.regex)
                    return (
                      <Chip
                        key={p.key}
                        size="small"
                        label={p.label}
                        color={selected ? 'primary' : 'default'}
                        variant={selected ? 'filled' : 'outlined'}
                        onClick={() => togglePreset(p.regex)}
                      />
                    )
                  })}
                </Stack>
              </Box>
              <TextField
                label="自定义正则（可选）"
                value={customRegex}
                onChange={(e) => setCustomRegex(e.target.value)}
                sx={{ width: 420 }}
              />
              <Typography variant="caption" color="text.secondary">
                勾选预设或填写自定义正则，命中任一即送 AI 分析
              </Typography>
            </Stack>
          )}
        </Box>
        <Box>
          <Typography variant="body2" gutterBottom>
            消息合并窗口：{mergeWindow} 秒（发送人停止发言 {mergeWindow} 秒后立即分析，默认 15 秒）
          </Typography>
          <Slider
            value={mergeWindow}
            min={5}
            max={600}
            step={5}
            marks
            valueLabelDisplay="auto"
            onChange={(_, v) => setMergeWindow(v as number)}
            sx={{ width: 320 }}
          />
        </Box>
        <Box>
          <Typography variant="body2" gutterBottom>
            商机合并窗口：{oppMergeWindow} 秒（同一人 {oppMergeWindow} 秒内再次命中的商机合并为一条，默认 180 秒；单条商机最长跨度 30 分钟）
          </Typography>
          <Slider
            value={oppMergeWindow}
            min={30}
            max={1800}
            step={30}
            marks
            valueLabelDisplay="auto"
            onChange={(_, v) => setOppMergeWindow(v as number)}
            sx={{ width: 320 }}
          />
        </Box>
        <Button
          variant="contained"
          disabled={saveAnalysisMutation.isPending}
          onClick={() => saveAnalysisMutation.mutate()}
        >
          保存分析设置
        </Button>
      </Stack>

      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle2" sx={{ mb: 1 }}>通知</Typography>
      <Stack spacing={2}>
        <FormControlLabel
          control={
            <Switch
              checked={soundEnabled}
              onChange={(e) => saveNotifyMutation.mutate(e.target.checked)}
            />
          }
          label="新商机提示音"
        />
        <Box>
          <Typography variant="body2" gutterBottom>
            提示音音量：{volume}（默认 60）
          </Typography>
          <Slider
            value={volume}
            min={0}
            max={100}
            step={5}
            marks
            valueLabelDisplay="auto"
            onChange={(_, v) => changeVolume(v as number)}
            sx={{ width: 320 }}
          />
        </Box>
        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={systemNotifyEnabled}
                onChange={(e) => toggleSystemNotify(e.target.checked)}
              />
            }
            label="系统通知（页面不在前台时收到新商机，系统弹窗提醒）"
          />
          {showAuthButton && (
            <Button
              variant="outlined"
              sx={{ mt: 1 }}
              onClick={() => void requestNotification()}
            >
              授权系统通知
            </Button>
          )}
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
            {notifPermissionLabel}；系统通知在页面切换到后台时才会发出，可在浏览器设置中撤销授权
          </Typography>
        </Box>
      </Stack>
    </Box>
  )
}

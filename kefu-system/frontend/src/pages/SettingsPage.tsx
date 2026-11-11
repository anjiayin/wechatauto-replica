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
  BillingOut,
  CacheDirInfo,
  LastPurge,
  MonitorSettings,
  MonitorTarget,
  OpportunityType,
  PurgeResult,
  RetentionSettings,
  SessionItem,
  StatusOut,
} from '../api/types'
import {
  PRESCREEN_PRESETS,
  mergePrescreen,
  parsePrescreen,
} from '../constants/prescreen'

/** 设置页：监控对象 / 商机类型 / AI 接口 / 数据与账单 四 tab。 */
export default function SettingsPage() {
  const [tab, setTab] = useState(0)
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Tabs value={tab} onChange={(_, v: number) => setTab(v)} sx={{ mb: 2 }}>
        <Tab label="监控对象" />
        <Tab label="商机类型" />
        <Tab label="AI 接口" />
        <Tab label="数据与账单" />
      </Tabs>
      {tab === 0 && <TargetsTab />}
      {tab === 1 && <TypesTab />}
      {tab === 2 && <AiTab />}
      {tab === 3 && <DataBillingTab />}
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
  const [disableThinking, setDisableThinking] = useState(true)
  // 是否使用 AI 接口（关闭 = 轻量筛选模式：关键词/正则命中即商机）
  const [aiEnabled, setAiEnabled] = useState(true)
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
    setDisableThinking(ai.disable_thinking !== false)
    setAiEnabled(ai.enabled !== false)
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
        {/* AI 开关（反馈 2）：关闭后不调用任何 AI，改用轻量筛选规则判定商机 */}
        <FormControlLabel
          control={
            <Switch
              checked={aiEnabled}
              onChange={(e) => setAiEnabled(e.target.checked)}
            />
          }
          label="使用 AI 接口分析商机（关闭后不调用 AI：改用「分析判定 → 轻量筛选」的关键词/正则，命中即算商机）"
        />
        {!aiEnabled && (
          <Alert severity="warning">
            已关闭 AI 接口：系统不再向任何第三方发送消息内容。
            请务必在下方「分析判定 → 轻量筛选」中填写关键词或正则，否则不会产生任何商机。
          </Alert>
        )}
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
              checked={disableThinking}
              onChange={(e) => setDisableThinking(e.target.checked)}
            />
          }
          label="关闭思考模式（推荐开启）：判定只需固定 JSON，思考型模型会额外生成大量隐藏推理内容，单条分析可能从 3 秒变成 30~60 秒。对所有模型生效，会按模型自动匹配参数（thinking / enable_thinking / reasoning_effort），「测试连接」结果里会显示实际生效的参数"
        />
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
            disabled={
              saveAiMutation.isPending ||
              (aiEnabled && (!baseUrl.trim() || !model.trim()))
            }
            onClick={() =>
              saveAiMutation.mutate({
                base_url: baseUrl.trim(),
                api_key: apiKey.trim(),
                model: model.trim(),
                timeout_sec: timeoutSec,
                temperature,
                disable_thinking: disableThinking,
                enabled: aiEnabled,
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
            label={
              aiEnabled
                ? '启用轻量预筛（关键词/正则未命中不调用 AI，节省成本）'
                : '轻量筛选规则（已关闭 AI：命中关键词/正则即算商机，无需开启上方预筛开关）'
            }
          />
          {(prescreenEnabled || !aiEnabled) && (
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

// ---------------------------------------------------------------------------
// Tab 4：数据与账单（反馈 7 历史数据管理 / 反馈 13 AI 账单估算）
// ---------------------------------------------------------------------------
const RETENTION_PRESETS = [
  { label: '1 个月', days: 30 },
  { label: '半年', days: 182 },
  { label: '1 年', days: 365 },
]

/** 清理目标表 → 中文名（提示文案用）。 */
function purgeTableLabel(table: string): string {
  return table === 'analysis_logs' ? '分析日志' : table === 'opportunities' ? '商机' : table
}

/** 字节数 → 人类可读（缓存目录占用展示用）。 */
function fmtBytes(n: number): string {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = n
  let idx = 0
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024
    idx += 1
  }
  return `${value.toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`
}

/** 清理预览结果 + 本次请求的保留天数（确认对话框真删时复用）。 */
interface PurgeConfirm extends PurgeResult {
  keep_days: number
}

function DataBillingTab() {
  const queryClient = useQueryClient()
  const [snack, setSnack] = useState<string | null>(null)
  // 待确认的清理预览（非空时弹二次确认对话框）
  const [pendingPurge, setPendingPurge] = useState<PurgeConfirm | null>(null)

  // 保留时长配置
  const { data: retention } = useQuery({
    queryKey: ['retention'],
    queryFn: () => apiGet<RetentionSettings>('/api/system/retention'),
  })
  const [logsDays, setLogsDays] = useState('')
  const [oppsDays, setOppsDays] = useState('')
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    if (retention && !loaded) {
      setLogsDays(String(retention.logs_days))
      setOppsDays(String(retention.opportunities_days))
      setLoaded(true)
    }
  }, [retention, loaded])

  const saveRetentionMutation = useMutation({
    mutationFn: () =>
      apiPut<RetentionSettings>('/api/system/retention', {
        logs_days: Number(logsDays),
        opportunities_days: Number(oppsDays),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['retention'] })
      setSnack('保留时长已保存')
    },
    onError: (e: Error) => setSnack(`保存失败：${e.message}`),
  })

  // 立即清理：先 dry_run 预览条数 → 二次确认 → 真删（防误删一整天的数据）
  const previewPurgeMutation = useMutation({
    mutationFn: (p: { table: string; keep_days: number }) =>
      apiPost<PurgeResult>('/api/system/purge', { ...p, dry_run: true }),
    onSuccess: (r, vars) => {
      if (r.deleted <= 0) {
        setSnack('没有需要清理的记录')
        return
      }
      setPendingPurge({ ...r, keep_days: vars.keep_days })
    },
    onError: (e: Error) => setSnack(`预览失败：${e.message}`),
  })

  const purgeMutation = useMutation({
    mutationFn: (p: { table: string; keep_days: number }) =>
      apiPost<PurgeResult>('/api/system/purge', { ...p, dry_run: false }),
    onSuccess: (r) => {
      setPendingPurge(null)
      void queryClient.invalidateQueries({ queryKey: ['purge-last'] })
      setSnack(`已清理${purgeTableLabel(r.table)} ${r.deleted} 条`)
    },
    onError: (e: Error) => {
      setPendingPurge(null)
      setSnack(`清理失败：${e.message}`)
    },
  })

  // 上次清理记录（后端从 settings 读取，不依赖日志文件）
  const { data: lastPurge } = useQuery({
    queryKey: ['purge-last'],
    queryFn: () => apiGet<LastPurge>('/api/system/purge/last'),
  })

  // 微信数据库解密缓存目录（默认客服系统 data/wechat_cache，不占 %TEMP%）
  const { data: cacheInfo } = useQuery({
    queryKey: ['cache-dir'],
    queryFn: () => apiGet<CacheDirInfo>('/api/system/cache'),
  })
  const [cacheDir, setCacheDir] = useState('')
  const [cacheLoaded, setCacheLoaded] = useState(false)
  useEffect(() => {
    if (cacheInfo && !cacheLoaded) {
      // 配置值为空 → 输入框留空，placeholder 显示默认目录
      setCacheDir(cacheInfo.configured || '')
      setCacheLoaded(true)
    }
  }, [cacheInfo, cacheLoaded])

  const saveCacheDirMutation = useMutation({
    mutationFn: (dir: string) =>
      apiPut<CacheDirInfo>('/api/system/cache', { dir }),
    onSuccess: (info) => {
      void queryClient.invalidateQueries({ queryKey: ['cache-dir'] })
      setCacheDir(info.configured || '')
      setSnack('缓存目录已保存，重启服务后生效')
    },
    onError: (e: Error) => setSnack(`保存失败：${e.message}`),
  })

  // AI 账单
  const { data: billing } = useQuery({
    queryKey: ['billing'],
    queryFn: () => apiGet<BillingOut>('/api/system/billing'),
  })
  const [inputPrice, setInputPrice] = useState('')
  const [outputPrice, setOutputPrice] = useState('')
  const [priceUnit, setPriceUnit] = useState<'per_k' | 'per_m'>('per_k')
  const [billingLoaded, setBillingLoaded] = useState(false)
  useEffect(() => {
    if (billing && !billingLoaded) {
      setInputPrice(billing.input_price ? String(billing.input_price) : '')
      setOutputPrice(billing.output_price ? String(billing.output_price) : '')
      setPriceUnit(billing.unit)
      setBillingLoaded(true)
    }
  }, [billing, billingLoaded])

  const saveBillingMutation = useMutation({
    mutationFn: () =>
      apiPost<{ ok: boolean }>('/api/settings/billing', {
        input_price: Number(inputPrice) || 0,
        output_price: Number(outputPrice) || 0,
        unit: priceUnit,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['billing'] })
      setSnack('AI 单价已保存，费用估算已更新')
    },
    onError: (e: Error) => setSnack(`保存失败：${e.message}`),
  })

  const unitLabel = priceUnit === 'per_k' ? '元 / 千 tokens' : '元 / 百万 tokens'

  return (
    <Box>
      <Typography variant="subtitle2" sx={{ mb: 1 }}>历史数据管理</Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
        分别配置分析日志与商机的保留天数，含义为「保留最近 N 天（含今天）」：填 1 只保留今天，
        填 2 保留昨天与今天。系统不再自动清理，只在你点击「立即清理」时按保留天数删除；删除不可恢复，请谨慎设置。
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
        {lastPurge && lastPurge.at
          ? `上次清理：${lastPurge.at} · ${purgeTableLabel(lastPurge.table ?? '')} · 删除 ${lastPurge.deleted ?? 0} 条 · 截断点 ${lastPurge.cutoff || '—'}`
          : '上次清理：暂无清理记录'}
      </Typography>
      <Stack spacing={2} sx={{ mb: 2 }}>
        <Stack direction="row" spacing={1} alignItems="center" useFlexGap>
          <Typography variant="body2" sx={{ width: 90 }}>分析日志保留</Typography>
          <TextField
            size="small"
            sx={{ width: 110 }}
            value={logsDays}
            onChange={(e) => setLogsDays(e.target.value.replace(/[^0-9]/g, ''))}
            InputProps={{ endAdornment: <InputAdornment position="end">天</InputAdornment> }}
          />
          {RETENTION_PRESETS.map((p) => (
            <Button
              key={p.days}
              size="small"
              variant={Number(logsDays) === p.days ? 'contained' : 'outlined'}
              onClick={() => setLogsDays(String(p.days))}
            >
              {p.label}
            </Button>
          ))}
          <Button
            size="small"
            color="warning"
            disabled={previewPurgeMutation.isPending || purgeMutation.isPending || !Number(logsDays)}
            onClick={() => previewPurgeMutation.mutate({ table: 'analysis_logs', keep_days: Number(logsDays) })}
          >
            立即清理
          </Button>
        </Stack>
        <Stack direction="row" spacing={1} alignItems="center" useFlexGap>
          <Typography variant="body2" sx={{ width: 90 }}>商机保留</Typography>
          <TextField
            size="small"
            sx={{ width: 110 }}
            value={oppsDays}
            onChange={(e) => setOppsDays(e.target.value.replace(/[^0-9]/g, ''))}
            InputProps={{ endAdornment: <InputAdornment position="end">天</InputAdornment> }}
          />
          {RETENTION_PRESETS.map((p) => (
            <Button
              key={p.days}
              size="small"
              variant={Number(oppsDays) === p.days ? 'contained' : 'outlined'}
              onClick={() => setOppsDays(String(p.days))}
            >
              {p.label}
            </Button>
          ))}
          <Button
            size="small"
            color="warning"
            disabled={previewPurgeMutation.isPending || purgeMutation.isPending || !Number(oppsDays)}
            onClick={() => previewPurgeMutation.mutate({ table: 'opportunities', keep_days: Number(oppsDays) })}
          >
            立即清理
          </Button>
        </Stack>
        <Button
          variant="contained"
          sx={{ alignSelf: 'flex-start' }}
          disabled={saveRetentionMutation.isPending}
          onClick={() => saveRetentionMutation.mutate()}
        >
          保存保留时长
        </Button>
      </Stack>

      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle2" sx={{ mb: 1 }}>微信数据库缓存目录</Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
        微信数据库需解密后才能读取，解密副本缓存在此目录（可增长到数百 MB）。默认放在客服系统运行目录下，
        不再使用系统临时目录（%TEMP%），避免被磁盘清理后需要全量重新解密、甚至重新提取密钥。
        修改后需重启服务生效，旧目录不会自动删除。
      </Typography>
      <Stack spacing={1.5}>
        <Stack direction="row" spacing={1} alignItems="center" useFlexGap>
          <TextField
            size="small"
            sx={{ flex: 1, minWidth: 320 }}
            label="缓存目录（留空 = 默认）"
            value={cacheDir}
            placeholder={cacheInfo?.default_dir ?? ''}
            onChange={(e) => setCacheDir(e.target.value)}
          />
          <Button
            variant="contained"
            disabled={saveCacheDirMutation.isPending}
            onClick={() => saveCacheDirMutation.mutate(cacheDir.trim())}
          >
            保存
          </Button>
          <Button
            disabled={saveCacheDirMutation.isPending || !cacheInfo?.configured}
            onClick={() => saveCacheDirMutation.mutate('')}
          >
            恢复默认
          </Button>
        </Stack>
        {cacheInfo && (
          <Box>
            <Typography variant="caption" color="text.secondary" component="div">
              生效目录：{cacheInfo.dir}（{fmtBytes(cacheInfo.size_bytes)}，{cacheInfo.file_count} 个文件）
              {cacheInfo.is_default ? ' · 默认' : ' · 自定义'}
            </Typography>
            <Typography variant="caption" color="text.secondary" component="div">
              默认目录：{cacheInfo.default_dir}
            </Typography>
            {cacheInfo.active_dir && (
              <Typography variant="caption" color="text.secondary" component="div">
                监听实际使用：{cacheInfo.active_dir}
              </Typography>
            )}
          </Box>
        )}
        {!!cacheInfo?.active_dir && !cacheInfo.active && (
          <Alert severity="warning" sx={{ py: 0.5 }}>
            配置已保存但尚未生效：当前监听仍在使用 {cacheInfo.active_dir}，重启服务后切换到新目录。
          </Alert>
        )}
        {!!cacheInfo?.legacy_dirs?.length && (
          <Alert severity="info" sx={{ py: 0.5 }}>
            检测到旧版临时目录缓存：{cacheInfo.legacy_dirs.join('、')}
            。确认新目录运行正常后可手动删除以释放空间（密钥缓存已自动复制到新目录）。
          </Alert>
        )}
      </Stack>

      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle2" sx={{ mb: 1 }}>AI 账单估算</Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.5 }}>
        填写所用 AI 接口的输入/输出单价，系统按每次调用的实际 token 用量自动累计每日估算费用（仅本机估算，非官方账单）。
        用量单独存档，清理分析日志不会影响账单历史；显示 ¥0.00 表示当天没有产生 AI 调用。
      </Typography>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }} useFlexGap>
        <TextField
          label="输入单价"
          size="small"
          sx={{ width: 120 }}
          value={inputPrice}
          onChange={(e) => setInputPrice(e.target.value.replace(/[^0-9.]/g, ''))}
        />
        <TextField
          label="输出单价"
          size="small"
          sx={{ width: 120 }}
          value={outputPrice}
          onChange={(e) => setOutputPrice(e.target.value.replace(/[^0-9.]/g, ''))}
        />
        <TextField
          select
          label="单位"
          size="small"
          sx={{ width: 160 }}
          value={priceUnit}
          onChange={(e) => setPriceUnit(e.target.value as 'per_k' | 'per_m')}
        >
          <MenuItem value="per_k">元 / 千 tokens</MenuItem>
          <MenuItem value="per_m">元 / 百万 tokens</MenuItem>
        </TextField>
        <Button
          variant="contained"
          disabled={saveBillingMutation.isPending}
          onClick={() => saveBillingMutation.mutate()}
        >
          保存单价
        </Button>
      </Stack>
      {billing && (
        <Box sx={{ bgcolor: '#f7f7f7', borderRadius: 1, p: 1.5 }}>
          <Typography variant="body2" sx={{ mb: 0.5 }}>
            今日：{billing.today.calls} 次调用 ·
            输入 {billing.today.prompt_tokens.toLocaleString()} tokens ·
            输出 {billing.today.completion_tokens.toLocaleString()} tokens ·
            估算费用 <b>¥{billing.today.cost.toFixed(4)}</b>（{unitLabel}）
          </Typography>
          {billing.series.length > 0 && (
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              近 7 日：{billing.series.map((d) => `${d.date.slice(5)} ¥${d.cost.toFixed(2)}`).join(' · ')}
            </Typography>
          )}
        </Box>
      )}

      <Dialog open={pendingPurge !== null} onClose={() => setPendingPurge(null)} maxWidth="xs" fullWidth>
        <DialogTitle>确认清理</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            将删除 {pendingPurge?.deleted ?? 0} 条早于 {pendingPurge?.cutoff || '—'} 的
            {purgeTableLabel(pendingPurge?.table ?? '')}记录，不可恢复。
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
            保留最近 {pendingPurge?.keep_days ?? 0} 天（含今天）：该时刻之后的记录会保留，请确认后再执行。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPendingPurge(null)}>取消</Button>
          <Button
            color="warning"
            variant="contained"
            disabled={purgeMutation.isPending}
            onClick={() => {
              if (!pendingPurge) return
              purgeMutation.mutate({
                table: pendingPurge.table,
                keep_days: pendingPurge.keep_days,
              })
            }}
          >
            确认删除
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={snack !== null}
        autoHideDuration={4000}
        onClose={() => setSnack(null)}
        message={snack}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      />
    </Box>
  )
}

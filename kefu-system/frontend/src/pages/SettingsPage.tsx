import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Box,
  Button,
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
import PersonAddIcon from '@mui/icons-material/PersonAdd'
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
  MonitorTarget,
  OpportunityType,
  SessionItem,
} from '../api/types'

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
// Tab 1：监控对象
// ---------------------------------------------------------------------------
function TargetsTab() {
  const queryClient = useQueryClient()
  const [keyword, setKeyword] = useState('')
  const [searched, setSearched] = useState<SessionItem[]>([])
  const [error, setError] = useState('')

  const { data: targets } = useQuery({
    queryKey: ['monitor-targets'],
    queryFn: () => apiGet<MonitorTarget[]>('/api/monitor-targets'),
  })

  const searchMutation = useMutation({
    mutationFn: () => apiGet<SessionItem[]>(`/api/sessions/search${qs({ keyword, limit: 20 })}`),
    onSuccess: (items) => setSearched(items),
    onError: (err: Error) => setError(err.message),
  })

  const addMutation = useMutation({
    mutationFn: (username: string) =>
      apiPost<MonitorTarget>('/api/monitor-targets', { username }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['monitor-targets'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const toggleMutation = useMutation({
    mutationFn: (t: MonitorTarget) =>
      apiPatch<MonitorTarget>(`/api/monitor-targets/${t.id}`, {
        enabled: t.enabled === 0,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['monitor-targets'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (t: MonitorTarget) => apiDelete(`/api/monitor-targets/${t.id}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['monitor-targets'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
    onError: (err: Error) => setError(err.message),
  })

  const targetUsernames = new Set((targets ?? []).map((t) => t.username))

  return (
    <Box>
      {error && (
        <Alert severity="error" sx={{ mb: 1 }} onClose={() => setError('')}>
          {error}
        </Alert>
      )}
      <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
        <TextField
          label="按昵称/群名搜索会话"
          sx={{ width: 320 }}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') searchMutation.mutate()
          }}
          InputProps={{
            endAdornment: (
              <InputAdornment position="end">
                <IconButton size="small" onClick={() => searchMutation.mutate()}>
                  <SearchIcon />
                </IconButton>
              </InputAdornment>
            ),
          }}
        />
        <Button
          variant="contained"
          onClick={() => searchMutation.mutate()}
          disabled={searchMutation.isPending}
        >
          搜索
        </Button>
      </Stack>

      {searched.length > 0 && (
        <>
          <Typography variant="subtitle2" sx={{ mt: 1 }}>候选会话（点击加入监控）</Typography>
          <List dense sx={{ maxHeight: 220, overflow: 'auto', border: '1px solid #eee', borderRadius: 1, mb: 2 }}>
            {searched.map((s) => (
              <ListItem
                key={s.username}
                secondaryAction={
                  targetUsernames.has(s.username) ? (
                    <Typography variant="caption" color="text.secondary">已监控</Typography>
                  ) : (
                    <Tooltip title="加入监控">
                      <IconButton size="small" onClick={() => addMutation.mutate(s.username)}>
                        <PersonAddIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )
                }
              >
                <ListItemText
                  primary={s.display_name}
                  secondary={`${s.kind === 'group' ? '群聊' : '好友'} · ${s.username}`}
                />
              </ListItem>
            ))}
            {searched.length === 0 && (
              <ListItem><ListItemText primary="未找到匹配的会话" /></ListItem>
            )}
          </List>
        </>
      )}

      <Divider sx={{ my: 1 }} />
      <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
        已配置监控对象（{targets?.length ?? 0}）
      </Typography>
      <List dense>
        {(targets ?? []).map((t) => (
          <ListItem
            key={t.id}
            secondaryAction={
              <Stack direction="row" spacing={1} alignItems="center">
                <Switch
                  checked={t.enabled === 1}
                  onChange={() => toggleMutation.mutate(t)}
                  size="small"
                />
                <IconButton size="small" onClick={() => deleteMutation.mutate(t)}>
                  <DeleteIcon fontSize="small" />
                </IconButton>
              </Stack>
            }
          >
            <ListItemText
              primary={t.display_name}
              secondary={`${t.kind === 'group' ? '群聊' : '好友'} · ${t.username}${t.enabled === 1 ? '' : '（已停用）'}`}
            />
          </ListItem>
        ))}
        {(targets ?? []).length === 0 && (
          <ListItem>
            <ListItemText
              primary="暂无监控对象"
              secondary="搜索并加入群/好友后，系统将实时分析其新消息"
            />
          </ListItem>
        )}
      </List>
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
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="body2" color="text.secondary">
          用自然语言描述你的商机口径，描述修改后立即对后续消息生效。
          示例：「寻找机械加工订单的采购需求——客户明确提到需要 CNC/车铣/钣金加工、数量、交期等」
        </Typography>
        <Button
          variant="contained"
          startIcon={<AddIcon />}
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
  const [regex, setRegex] = useState('')
  const [mergeWindow, setMergeWindow] = useState(300)
  const [analysisLoaded, setAnalysisLoaded] = useState(false)
  if (analysis && !analysisLoaded) {
    setStrictness(analysis.strictness)
    setMinConfidence(analysis.min_confidence)
    setPrescreenEnabled(analysis.prescreen_enabled)
    setKeywords(analysis.prescreen_keywords)
    setRegex(analysis.prescreen_regex)
    setMergeWindow(analysis.merge_window_sec)
    setAnalysisLoaded(true)
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
        prescreen_regex: regex,
        merge_window_sec: mergeWindow,
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
    },
    onError: (err: Error) => setError(err.message),
  })

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
                label="预筛关键词（逗号分隔）"
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                sx={{ width: 420 }}
              />
              <TextField
                label="预筛正则（可选）"
                value={regex}
                onChange={(e) => setRegex(e.target.value)}
                sx={{ width: 420 }}
              />
            </Stack>
          )}
        </Box>
        <Box>
          <Typography variant="body2" gutterBottom>
            合并窗口：{mergeWindow} 秒（同一人连续发言静默该时长后合并为一条分析单元，商机最快在该时延后可见）
          </Typography>
          <Slider
            value={mergeWindow}
            min={30}
            max={1800}
            step={30}
            marks
            valueLabelDisplay="auto"
            onChange={(_, v) => setMergeWindow(v as number)}
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
      <FormControlLabel
        control={
          <Switch
            checked={soundEnabled}
            onChange={(e) => saveNotifyMutation.mutate(e.target.checked)}
          />
        }
        label="新商机提示音"
      />
    </Box>
  )
}

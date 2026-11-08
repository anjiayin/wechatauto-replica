import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  IconButton,
  MenuItem,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import KeyIcon from '@mui/icons-material/Key'
import BookmarkAddIcon from '@mui/icons-material/BookmarkAdd'
import DeleteSweepIcon from '@mui/icons-material/DeleteSweep'
import ManageAccountsIcon from '@mui/icons-material/ManageAccounts'
import EditIcon from '@mui/icons-material/Edit'
import DeleteIcon from '@mui/icons-material/Delete'
import AddIcon from '@mui/icons-material/Add'
import { apiGet, apiPut, qs } from '../api/client'
import type { AnalysisLogItem, AnalysisLogListOut, AnalysisSettings } from '../api/types'

const STATUS_LABELS: Record<string, string> = {
  ok: '成功',
  skipped: '跳过',
  error: '失败',
  below_threshold: '低于门槛',
}

const STATUS_COLORS: Record<string, 'success' | 'default' | 'error' | 'warning'> = {
  ok: 'success',
  skipped: 'default',
  error: 'error',
  below_threshold: 'warning',
}

/** 去掉行首 "[HH:MM] " 前缀（漏判修正预填用） */
function stripTimePrefix(text: string): string {
  return String(text ?? '')
    .split('\n')
    .map((l) => l.replace(/^\[\d{1,2}:\d{2}\]\s*/, ''))
    .join(' ')
    .trim()
}

/** AI 结果摘要（ok 显示命中/类型/置信度，skipped/error 显示原因） */
function summarizeResult(log: AnalysisLogItem): string {
  if (log.status === 'ok' || log.status === 'below_threshold') {
    try {
      const r = JSON.parse(log.result || '') as {
        hit?: boolean
        type?: string | null
        confidence?: number
      }
      if (r && typeof r === 'object') {
        if (r.hit) {
          const suffix = log.status === 'below_threshold' ? '，低于门槛' : ''
          return `命中 ${r.type ?? '未知类型'}（置信度 ${r.confidence ?? 0}${suffix}）`
        }
        return 'AI 未命中'
      }
    } catch {
      // 结果 JSON 解析失败 → 退回通用文案
    }
    return log.status === 'below_threshold' ? '低于门槛' : '—'
  }
  return log.error || '—'
}

/** 该条记录是否可做漏判修正（预筛跳过 / AI 未命中） */
function isCorrectable(log: AnalysisLogItem): boolean {
  if (log.status === 'skipped') return true
  if (log.status !== 'ok') return false
  try {
    const r = JSON.parse(log.result || '') as { hit?: boolean }
    return !(r && r.hit)
  } catch {
    return true
  }
}

/** 分析日志页：筛选 + 表格 + 漏判修正（加为预筛关键词 / 作为商机示例）。 */
export default function AnalysisLogs() {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState('')
  const [sessionInput, setSessionInput] = useState('')
  const [session, setSession] = useState('')
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(20)
  const [snack, setSnack] = useState<{ msg: string; severity: 'success' | 'error' } | null>(null)
  const [managerOpen, setManagerOpen] = useState(false)

  // 筛选输入 300ms 防抖
  useEffect(() => {
    const t = window.setTimeout(() => {
      setSession(sessionInput.trim())
      setKeyword(keywordInput.trim())
    }, 300)
    return () => window.clearTimeout(t)
  }, [sessionInput, keywordInput])

  const query = useMemo(
    () =>
      `/api/analysis-logs${qs({
        page: page + 1,
        page_size: pageSize,
        status,
        session,
        keyword,
      })}`,
    [page, pageSize, status, session, keyword],
  )

  const { data, isLoading } = useQuery({
    queryKey: ['analysis-logs', query],
    queryFn: () => apiGet<AnalysisLogListOut>(query),
  })

  // 当前修正示例（顶部 n/20 + 清空按钮）
  const { data: analysis } = useQuery({
    queryKey: ['analysis-settings'],
    queryFn: () => apiGet<AnalysisSettings>('/api/settings/analysis'),
  })
  const examples = analysis?.correction_examples ?? []

  // 全量替换分析设置（漏判修正与清空示例共用：读当前值 → 合并 → PUT）
  const saveSettingsMutation = useMutation({
    mutationFn: async (patch: {
      prescreen_keywords?: string
      correction_examples?: string[]
    }) => {
      const current = await apiGet<AnalysisSettings>('/api/settings/analysis')
      return apiPut<AnalysisSettings>('/api/settings/analysis', {
        strictness: current.strictness,
        min_confidence: current.min_confidence,
        prescreen_enabled: current.prescreen_enabled,
        prescreen_keywords: current.prescreen_keywords,
        prescreen_regex: current.prescreen_regex,
        merge_window_sec: current.merge_window_sec,
        opp_merge_window_sec: current.opp_merge_window_sec,
        correction_examples: current.correction_examples ?? [],
        ...patch,
      })
    },
    onSuccess: (_resp, patch) => {
      void queryClient.invalidateQueries({ queryKey: ['analysis-settings'] })
      setSnack({
        msg: patch.correction_examples !== undefined && patch.correction_examples.length === 0
          ? '已清空全部商机示例'
          : '已保存，下次分析生效',
        severity: 'success',
      })
    },
    onError: (err: Error) => setSnack({ msg: err.message, severity: 'error' }),
  })

  const items = data?.items ?? []

  return (
    <Box>
      {/* 页面说明 + 示例管理 */}
      <Stack
        direction="row"
        spacing={1.5}
        alignItems="center"
        sx={{ mb: 1.5, flexWrap: 'wrap' }}
        useFlexGap
      >
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1, minWidth: 260 }}>
          漏判修正：把被预筛跳过或 AI 未命中的真实商机消息，加入关键词或示例，后续类似消息将能被识别。
        </Typography>
        <Chip
          size="small"
          color="primary"
          variant="outlined"
          label={`商机示例 ${examples.length}/20`}
        />
        <Button
          size="small"
          startIcon={<ManageAccountsIcon />}
          disabled={saveSettingsMutation.isPending}
          onClick={() => setManagerOpen(true)}
        >
          管理示例
        </Button>
        <Button
          size="small"
          startIcon={<DeleteSweepIcon />}
          disabled={examples.length === 0 || saveSettingsMutation.isPending}
          onClick={() => saveSettingsMutation.mutate({ correction_examples: [] })}
        >
          清空示例
        </Button>
      </Stack>

      {/* 示例管理对话框：逐条编辑 / 删除 / 新增（全量数组 PUT 保存） */}
      <ExamplesManagerDialog
        open={managerOpen}
        onClose={() => setManagerOpen(false)}
        examples={examples}
        onSave={(next) => saveSettingsMutation.mutate({ correction_examples: next })}
        saving={saveSettingsMutation.isPending}
      />

      {/* 筛选栏 */}
      <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: 'wrap' }} useFlexGap>
        <TextField
          select
          label="状态"
          sx={{ width: 130 }}
          value={status}
          onChange={(e) => {
            setPage(0)
            setStatus(e.target.value)
          }}
        >
          <MenuItem value="">全部</MenuItem>
          <MenuItem value="ok">成功</MenuItem>
          <MenuItem value="skipped">跳过</MenuItem>
          <MenuItem value="error">失败</MenuItem>
          <MenuItem value="below_threshold">低于门槛</MenuItem>
        </TextField>
        <TextField
          label="来源会话（username）"
          sx={{ width: 240 }}
          value={sessionInput}
          onChange={(e) => {
            setPage(0)
            setSessionInput(e.target.value)
          }}
        />
        <TextField
          label="消息内容关键字"
          sx={{ width: 240 }}
          value={keywordInput}
          onChange={(e) => {
            setPage(0)
            setKeywordInput(e.target.value)
          }}
        />
      </Stack>

      <Paper variant="outlined">
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>时间</TableCell>
                <TableCell>状态</TableCell>
                <TableCell>来源会话</TableCell>
                <TableCell align="right">消息条数</TableCell>
                <TableCell>消息内容</TableCell>
                <TableCell>AI 结果摘要</TableCell>
                <TableCell align="right">AI 耗时</TableCell>
                <TableCell align="right">操作</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {isLoading && (
                <TableRow>
                  <TableCell colSpan={8}>加载中…</TableCell>
                </TableRow>
              )}
              {!isLoading && items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={8}>
                    <Typography color="text.secondary" align="center" sx={{ py: 4 }}>
                      暂无分析日志。产生新消息分析记录后这里会展示，可对漏判记录做修正。
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
              {items.map((log) => (
                <LogRow
                  key={log.id}
                  log={log}
                  onSaveKeyword={(kw) =>
                    saveSettingsMutation.mutate({
                      prescreen_keywords: appendKeyword(
                        analysis?.prescreen_keywords ?? '',
                        kw,
                      ),
                    })
                  }
                  onSaveExample={(text) => {
                    const next = [...examples, text]
                    saveSettingsMutation.mutate({ correction_examples: next })
                  }}
                  examplesFull={examples.length >= 20}
                />
              ))}
            </TableBody>
          </Table>
        </Box>
        <TablePagination
          component="div"
          count={data?.total ?? 0}
          page={page}
          rowsPerPage={pageSize}
          rowsPerPageOptions={[10, 20, 50]}
          labelRowsPerPage="每页"
          onRowsPerPageChange={(e) => {
            setPageSize(Number(e.target.value))
            setPage(0)
          }}
          onPageChange={(_, p) => setPage(p)}
        />
      </Paper>

      <Snackbarish snack={snack} onClose={() => setSnack(null)} />
    </Box>
  )
}

/** 关键词合并：拆分（中英文逗号均可）→ 去重 → 中文逗号拼接展示 */
function appendKeyword(existing: string, kw: string): string {
  const list = existing
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (!list.includes(kw)) list.push(kw)
  return list.join('，')
}

/** 日志行 + 漏判修正操作对话框（每行独立状态，避免互串） */
function LogRow(props: {
  log: AnalysisLogItem
  onSaveKeyword: (kw: string) => void
  onSaveExample: (text: string) => void
  examplesFull: boolean
}) {
  const { log } = props
  const correctable = isCorrectable(log)
  // ① 加为预筛关键词
  const [kwOpen, setKwOpen] = useState(false)
  const [kwText, setKwText] = useState('')
  // ② 作为商机示例
  const [exOpen, setExOpen] = useState(false)

  const openKeywordDialog = () => {
    setKwText(stripTimePrefix(log.input_text).slice(0, 30))
    setKwOpen(true)
  }

  return (
    <TableRow hover>
      <TableCell sx={{ whiteSpace: 'nowrap' }}>
        {log.created_at.replace('T', ' ')}
      </TableCell>
      <TableCell>
        <Chip
          size="small"
          color={STATUS_COLORS[log.status] ?? 'default'}
          label={STATUS_LABELS[log.status] ?? log.status}
        />
      </TableCell>
      <TableCell>
        <Tooltip title={log.session_username} placement="top">
          <Typography variant="body2" noWrap sx={{ maxWidth: 140 }}>
            {log.session_display || log.session_username}
          </Typography>
        </Tooltip>
      </TableCell>
      <TableCell align="right">{log.msg_count}</TableCell>
      <TableCell sx={{ maxWidth: 300, minWidth: 160 }}>
        <Tooltip title={log.input_text} placement="top" arrow>
          <Typography
            variant="body2"
            sx={{
              display: '-webkit-box',
              WebkitBoxOrient: 'vertical',
              WebkitLineClamp: 2,
              overflow: 'hidden',
            }}
          >
            {log.input_text_display || log.input_text || '—'}
          </Typography>
        </Tooltip>
      </TableCell>
      <TableCell sx={{ maxWidth: 260 }}>
        <Typography
          variant="body2"
          color={log.status === 'error' ? 'error' : 'text.primary'}
          sx={{ whiteSpace: 'pre-line' }}
        >
          {summarizeResult(log)}
        </Typography>
      </TableCell>
      <TableCell align="right">
        {log.latency_ms !== null ? `${log.latency_ms} ms` : '—'}
      </TableCell>
      <TableCell align="right">
        {correctable && (
          <Stack direction="row" spacing={0.5} justifyContent="flex-end">
            <Tooltip title="编辑为短关键词，追加到预筛关键词（去重）">
              <Button size="small" startIcon={<KeyIcon />} onClick={openKeywordDialog}>
                加为预筛关键词
              </Button>
            </Tooltip>
            <Tooltip title="追加为商机示例，相似消息后续将被识别">
              <Button
                size="small"
                startIcon={<BookmarkAddIcon />}
                onClick={() => setExOpen(true)}
              >
                作为商机示例
              </Button>
            </Tooltip>
          </Stack>
        )}
      </TableCell>

      {/* 对话框①：加为预筛关键词 */}
      <Dialog open={kwOpen} onClose={() => setKwOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>加为预筛关键词</DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            fullWidth
            label="预筛关键词"
            helperText="命中该关键词的消息将跳过更重的判断直接送 AI 分析（与现有关键词去重）"
            value={kwText}
            onChange={(e) => setKwText(e.target.value)}
            sx={{ mt: 1 }}
          />
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
            原始消息：{log.input_text}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setKwOpen(false)}>取消</Button>
          <Button
            variant="contained"
            disabled={!kwText.trim()}
            onClick={() => {
              props.onSaveKeyword(kwText.trim())
              setKwOpen(false)
            }}
          >
            保存
          </Button>
        </DialogActions>
      </Dialog>

      {/* 对话框②：作为商机示例 */}
      <Dialog open={exOpen} onClose={() => setExOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>作为商机示例</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 1 }}>
            {props.examplesFull
              ? '示例已满 20 条，保存后将淘汰最旧一条（FIFO）。'
              : '确认把以下消息追加为商机示例？保存后下次分析生效。'}
          </DialogContentText>
          <Box
            sx={{
              bgcolor: '#f7f7f7',
              border: '1px solid #eee',
              borderRadius: 1,
              p: 1.5,
              maxHeight: 200,
              overflow: 'auto',
            }}
          >
            <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
              {stripTimePrefix(log.input_text) || log.input_text || '（空）'}
            </Typography>
          </Box>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setExOpen(false)}>取消</Button>
          <Button
            variant="contained"
            onClick={() => {
              props.onSaveExample(stripTimePrefix(log.input_text))
              setExOpen(false)
            }}
          >
            确认追加
          </Button>
        </DialogActions>
      </Dialog>
    </TableRow>
  )
}

/** 示例上限（与后端 CORRECTION_EXAMPLES_MAX 对齐） */
const EXAMPLES_MAX = 20

/**
 * 商机示例管理对话框：列出全部示例（序号+文本），支持行内编辑、
 * 删除（确认）、底部新增；每次操作重组数组后全量 PUT 保存。
 */
function ExamplesManagerDialog(props: {
  open: boolean
  onClose: () => void
  examples: string[]
  onSave: (next: string[]) => void
  saving: boolean
}) {
  const { open, onClose, examples, onSave, saving } = props
  // 行内编辑状态：正在编辑的行下标与草稿文本（-1 = 无编辑中的行）
  const [editIndex, setEditIndex] = useState(-1)
  const [editText, setEditText] = useState('')
  // 新增输入
  const [addText, setAddText] = useState('')
  // 删除确认
  const [deleteIndex, setDeleteIndex] = useState(-1)
  const full = examples.length >= EXAMPLES_MAX

  const startEdit = (index: number, text: string) => {
    setEditIndex(index)
    setEditText(text)
  }

  const commitEdit = () => {
    const text = editText.trim()
    if (!text || editIndex < 0) return
    const next = examples.map((e, i) => (i === editIndex ? text : e))
    onSave(next)
    setEditIndex(-1)
    setEditText('')
  }

  const confirmDelete = () => {
    if (deleteIndex < 0) return
    onSave(examples.filter((_, i) => i !== deleteIndex))
    setDeleteIndex(-1)
  }

  const handleAdd = () => {
    const text = addText.trim()
    if (!text || full) return
    onSave([...examples, text])
    setAddText('')
  }

  const handleClose = () => {
    setEditIndex(-1)
    setEditText('')
    setAddText('')
    setDeleteIndex(-1)
    onClose()
  }

  return (
    <>
      <Dialog open={open} onClose={handleClose} fullWidth maxWidth="sm">
        <DialogTitle>管理商机示例（{examples.length}/{EXAMPLES_MAX}）</DialogTitle>
        <DialogContent>
          {examples.length === 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
              暂无示例。可在下方新增，或在分析日志列表对漏判记录点「作为商机示例」。
            </Typography>
          )}
          {examples.map((text, index) => (
            <Box
              key={`${index}-${text.slice(0, 16)}`}
              sx={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 1,
                py: 1,
                borderBottom: '1px dashed #eee',
              }}
            >
              <Typography variant="caption" color="text.secondary" sx={{ pt: 1.2, minWidth: 24 }}>
                {index + 1}.
              </Typography>
              {editIndex === index ? (
                <TextField
                  fullWidth
                  size="small"
                  multiline
                  minRows={2}
                  value={editText}
                  autoFocus
                  onChange={(e) => setEditText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.ctrlKey && e.key === 'Enter') commitEdit()
                  }}
                />
              ) : (
                <Typography
                  variant="body2"
                  sx={{
                    flex: 1,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    pt: 0.5,
                  }}
                >
                  {text}
                </Typography>
              )}
              <Box sx={{ display: 'flex', gap: 0.5, flexShrink: 0 }}>
                {editIndex === index ? (
                  <>
                    <Button
                      size="small"
                      variant="contained"
                      disabled={!editText.trim() || saving}
                      onClick={commitEdit}
                    >
                      保存
                    </Button>
                    <Button size="small" onClick={() => setEditIndex(-1)}>
                      取消
                    </Button>
                  </>
                ) : (
                  <>
                    <IconButton
                      size="small"
                      disabled={saving}
                      onClick={() => startEdit(index, text)}
                    >
                      <EditIcon fontSize="small" />
                    </IconButton>
                    <IconButton
                      size="small"
                      disabled={saving}
                      onClick={() => setDeleteIndex(index)}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </>
                )}
              </Box>
            </Box>
          ))}

          {/* 新增示例 */}
          <Stack direction="row" spacing={1} sx={{ mt: 2 }} alignItems="flex-start">
            <TextField
              fullWidth
              size="small"
              multiline
              minRows={2}
              label={full ? '示例已满，无法新增' : '新增示例'}
              helperText={
                full
                  ? `已满 ${EXAMPLES_MAX} 条（FIFO 淘汰最旧），请先删除或编辑部分示例`
                  : '保存后下次分析生效（与现有示例去空、截断 200 字）'
              }
              value={addText}
              disabled={full}
              onChange={(e) => setAddText(e.target.value)}
            />
            <Button
              variant="contained"
              startIcon={<AddIcon />}
              disabled={full || !addText.trim() || saving}
              onClick={handleAdd}
              sx={{ mt: 0.5, flexShrink: 0 }}
            >
              新增
            </Button>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClose}>关闭</Button>
        </DialogActions>
      </Dialog>

      {/* 删除确认对话框 */}
      <Dialog
        open={deleteIndex >= 0}
        onClose={() => setDeleteIndex(-1)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>删除示例</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ wordBreak: 'break-all' }}>
            确定删除第 {deleteIndex + 1} 条示例「
            {examples[deleteIndex]?.slice(0, 60)}
            {examples[deleteIndex] && examples[deleteIndex].length > 60 ? '…' : ''}」？
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteIndex(-1)}>取消</Button>
          <Button
            color="error"
            variant="contained"
            disabled={saving}
            onClick={confirmDelete}
          >
            删除
          </Button>
        </DialogActions>
      </Dialog>
    </>
  )
}

/** 轻量提示条（Snackbar 顶部渲染于行组件外，避免行内嵌套报错） */
function Snackbarish(props: {
  snack: { msg: string; severity: 'success' | 'error' } | null
  onClose: () => void
}) {
  if (!props.snack) return null
  return (
    <Alert
      severity={props.snack.severity}
      onClose={props.onClose}
      sx={{ position: 'fixed', bottom: 24, left: '50%', transform: 'translateX(-50%)', zIndex: 1400 }}
    >
      {props.snack.msg}
    </Alert>
  )
}

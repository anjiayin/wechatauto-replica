/**
 * 客户端更新 UI：顶栏版本/更新提示、更新确认对话框、设置页「系统更新」标签。
 *
 * 后端已完成 manifest 验签与三级判定，前端只负责展示与「关杀软确认」交互；
 * 任何下载/切换都由后端接口执行，前端不直接访问 OSS（避免 CORS 与密钥问题）。
 */
import { useCallback, useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
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
  FormControlLabel,
  LinearProgress,
  Paper,
  Stack,
  Typography,
} from '@mui/material'
import SystemUpdateAltIcon from '@mui/icons-material/SystemUpdateAlt'
import { apiGet, apiPost } from '../api/client'
import type { UpdateCheckOut, UpdateStatusOut } from '../api/types'

const QUERY_KEY = ['update-check']

function hasUpdate(c?: UpdateCheckOut): boolean {
  return c?.action === 'optional' || c?.action === 'forced'
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function sizeText(bytes: number): string {
  return bytes > 0 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : '—'
}

/** 共享的检查状态：App 每 12h + 启动延迟 5s；设置页复用同一 queryKey。 */
export function useUpdateCheck(autoStart: boolean) {
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => apiGet<UpdateCheckOut>('/api/system/update/check'),
    enabled: false,
    staleTime: 5 * 60 * 1000,
    retry: false,
  })
  const refetch = query.refetch
  useEffect(() => {
    if (!autoStart) return
    const first = window.setTimeout(() => { void refetch() }, 5000)
    const timer = window.setInterval(() => { void refetch() }, 12 * 3600 * 1000)
    return () => {
      window.clearTimeout(first)
      window.clearInterval(timer)
    }
  }, [autoStart, refetch])
  return query
}

/** 顶栏：无更新显示当前版本，有更新显示醒目提示（点击打开详情）。 */
export function UpdateChip({ check, onClick }: {
  check?: UpdateCheckOut
  onClick: () => void
}) {
  const update = hasUpdate(check)
  return (
    <Chip
      size="small"
      icon={<SystemUpdateAltIcon />}
      label={update ? `有新版本 ${check!.latest}` : `v${__APP_VERSION__}`}
      color={update ? 'warning' : 'default'}
      onClick={onClick}
      sx={{
        mr: 1,
        fontWeight: 600,
        ...(update ? {} : {
          color: 'rgba(255,255,255,0.85)',
          bgcolor: 'rgba(255,255,255,0.12)',
        }),
      }}
    />
  )
}

/** 更新详情 + 关杀软确认 + 下载进度 + 安装切换。 */
export function UpdateDialog({ open, onClose, check }: {
  open: boolean
  onClose: () => void
  check?: UpdateCheckOut
}) {
  const qc = useQueryClient()
  const [agreed, setAgreed] = useState(false)
  const [status, setStatus] = useState<UpdateStatusOut | null>(null)
  const [error, setError] = useState('')
  const [polling, setPolling] = useState(false)
  const [applied, setApplied] = useState(false)

  const reset = useCallback(() => {
    setAgreed(false)
    setStatus(null)
    setError('')
    setPolling(false)
    setApplied(false)
  }, [])
  useEffect(() => {
    if (!open) {
      reset()
      return
    }
    // 重新打开时恢复后端已有的下载状态（例如上次下载完成后点了「稍后」）
    void apiGet<UpdateStatusOut>('/api/system/update/status')
      .then(setStatus)
      .catch(() => { /* 状态查询失败不打扰主流程 */ })
  }, [open, reset])

  // 下载期间轮询本地状态接口；终态自动停止（error/done/applied）。
  useEffect(() => {
    if (!open || !polling || applied) return
    let alive = true
    const tick = () => {
      apiGet<UpdateStatusOut>('/api/system/update/status')
        .then((s) => {
          if (!alive) return
          setStatus(s)
          if (s.state === 'done' || s.state === 'error' || s.state === 'applied') {
            setPolling(false)
          }
        })
        .catch((e) => {
          if (!alive) return
          setError(errText(e))
          setPolling(false)
        })
    }
    void tick()
    const timer = window.setInterval(() => { void tick() }, 700)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [open, polling, applied])

  const c = check
  const update = hasUpdate(c)
  const state = status?.state ?? 'idle'
  const downloading = state === 'downloading'
  const pct = status?.total
    ? Math.min(100, Math.round((status.done / status.total) * 100))
    : 0

  const startDownload = async () => {
    if (!c || !agreed) return
    setError('')
    setStatus(null)
    try {
      const r = await apiPost<{ ok: boolean }>(
        '/api/system/update/download', { confirmed: true },
      )
      if (!r.ok) throw new Error('后端未能开始下载')
      setPolling(true)
    } catch (e) {
      setError(errText(e))
    }
  }

  const applyUpdate = async () => {
    setError('')
    try {
      const r = await apiPost<{ ok: boolean; need_restart: boolean }>(
        '/api/system/update/apply',
      )
      if (!r.ok) throw new Error('后端未能完成安装')
      setApplied(true)
      qc.setQueryData<UpdateCheckOut>(QUERY_KEY,
        (old) => (old ? { ...old, action: 'none' } : old))
    } catch (e) {
      setError(errText(e))
    }
  }

  const ignoreVersion = async () => {
    if (!c) return
    try {
      await apiPost('/api/system/update/ignore', { version: c.latest })
      qc.setQueryData<UpdateCheckOut>(QUERY_KEY,
        (old) => (old ? { ...old, action: 'none' } : old))
      onClose()
    } catch (e) {
      setError(errText(e))
    }
  }

  return (
    <Dialog open={open} onClose={downloading ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>系统更新</DialogTitle>
      <DialogContent dividers>
        {!update && !applied && (
          <Alert severity="info" sx={{ mb: 2 }}>
            当前已经是最新版本（v{__APP_VERSION__}）
          </Alert>
        )}
        {update && c && (
          <>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <Typography variant="body1" fontWeight={600}>
                v{c.current_version} → v{c.latest}
              </Typography>
              {c.action === 'forced' && <Chip size="small" color="error" label="强制更新" />}
              {c.released_at && (
                <Typography variant="caption" color="text.secondary">
                  {c.released_at}
                </Typography>
              )}
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              下载大小：{sizeText(c.package_size)}
              {c.min_wechat_version ? ` · 需微信版本 ≥ ${c.min_wechat_version}` : ''}
            </Typography>
            {c.notes && (
              <Paper variant="outlined" sx={{ p: 1.5, mb: 2, bgcolor: 'grey.50' }}>
                <Typography variant="subtitle2" sx={{ mb: 0.5 }}>更新内容</Typography>
                <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                  {c.notes}
                </Typography>
              </Paper>
            )}
          </>
        )}

        {c?.requires_antivirus_off && update && !applied && (
          <Alert severity="warning" sx={{ mb: 1 }}>
            本程序未做代码签名。更新会写入新的程序文件，360 / 火绒 / Windows Defender
            等安全软件可能拦截。请先关闭安全软件，更新完成并能正常启动后再重新开启。
          </Alert>
        )}
        {update && !applied && !downloading && state !== 'done' && (
          <FormControlLabel
            control={<Checkbox checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />}
            label="我已关闭安全软件"
          />
        )}

        {(downloading || state === 'done') && (
          <Box sx={{ mt: 2 }}>
            <Stack direction="row" alignItems="center" spacing={1}>
              {downloading && <CircularProgress size={16} />}
              <Typography variant="body2">
                {state === 'done' ? '下载完成，等待安装' : '正在下载更新包…'}
                {status?.total ? ` ${pct}%` : ''}
              </Typography>
            </Stack>
            <LinearProgress variant="determinate" value={pct} sx={{ mt: 1 }} />
          </Box>
        )}
        {state === 'error' && (
          <Alert severity="error" sx={{ mt: 2 }}>
            更新失败：{status?.message || error}（已停在原版本，可关闭杀软后重试）
          </Alert>
        )}
        {applied && (
          <Alert severity="success" sx={{ mt: 2 }}>
            新版本已安装。请退出托盘程序后重新启动，即可使用新版本。
          </Alert>
        )}
        {error && !state.startsWith('error') && (
          <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={downloading}>
          {applied ? '关闭' : '稍后'}
        </Button>
        {update && c?.action !== 'forced' && !applied && !downloading && state !== 'done' && (
          <Button onClick={() => void ignoreVersion()} color="inherit">
            忽略此版本
          </Button>
        )}
        {update && !downloading && !applied && state !== 'done' && (
          <Button
            variant="contained"
            onClick={() => void startDownload()}
            disabled={!agreed}
          >
            {state === 'error' ? '重试下载' : '立即更新'}
          </Button>
        )}
        {state === 'done' && (
          <Button variant="contained" color="warning" onClick={() => void applyUpdate()}>
            安装并重启
          </Button>
        )}
      </DialogActions>
    </Dialog>
  )
}

/** 设置页「系统更新」标签：手动检查 + 版本信息（对话框复用）。 */
export function UpdateSettingsTab() {
  const q = useUpdateCheck(false)
  const [open, setOpen] = useState(false)
  const c = q.data
  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="subtitle1" fontWeight={600}>系统更新</Typography>
        <Chip size="small" label={`当前版本 v${__APP_VERSION__}`} />
        <Box sx={{ flexGrow: 1 }} />
        <Button
          size="small"
          onClick={() => void q.refetch()}
          disabled={q.isFetching}
        >
          {q.isFetching ? '检查中…' : '检查更新'}
        </Button>
        {hasUpdate(c) && (
          <Button size="small" variant="contained" onClick={() => setOpen(true)}>
            查看更新
          </Button>
        )}
      </Stack>
      <Paper variant="outlined" sx={{ p: 2 }}>
        {q.isFetching && <Typography variant="body2">正在检查更新…</Typography>}
        {!q.isFetching && !c && (
          <Typography variant="body2" color="text.secondary">
            尚未检查更新。点击「检查更新」从更新服务器获取版本信息。
          </Typography>
        )}
        {!q.isFetching && c && (
          <Stack spacing={1}>
            <Typography variant="body2">
              当前版本：v{c.current_version}
              {c.latest ? ` · 最新版本：v${c.latest}` : ''}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {hasUpdate(c)
                ? c.action === 'forced' ? '需要强制更新后才能继续使用' : '发现新版本，可点击「查看更新」安装'
                : `当前已经是最新版本（v${__APP_VERSION__}）`}
            </Typography>
            {c.notes && (
              <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                更新内容：{c.notes}
              </Typography>
            )}
          </Stack>
        )}
      </Paper>
      <UpdateDialog open={open} onClose={() => setOpen(false)} check={c} />
    </Box>
  )
}

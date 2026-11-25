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

/** 有更新但被用户主动忽略：action=none，后端会额外返回 ignored=true。 */
function isIgnored(c?: UpdateCheckOut): boolean {
  return c?.action === 'none' && c.ignored === true
}

/**
 * 版本号只认后端 current_version（运行时读 version.txt）。
 * __APP_VERSION__ 是前端编译期常量，用户漏跑 npm build 时它会落后于实际
 * 安装版本——现场曾同屏出现 v1.3.0 / v1.3.1 / v1.3.2 三个版本号。
 * 这里只在 check 尚未返回时用它兜底，绝不参与「已是最新版本」的展示。
 */
function currentVersion(c?: UpdateCheckOut): string {
  return c?.current_version || __APP_VERSION__
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
  const ignored = isIgnored(check)
  const label = update
    ? `有新版本 ${check!.latest}`
    : ignored ? `已忽略 ${check!.latest}` : `v${currentVersion(check)}`
  return (
    <Chip
      size="small"
      icon={<SystemUpdateAltIcon />}
      label={label}
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
  const [unignoring, setUnignoring] = useState(false)

  const reset = useCallback(() => {
    setAgreed(false)
    setStatus(null)
    setError('')
    setPolling(false)
    setApplied(false)
    setUnignoring(false)
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
          if (['done', 'error', 'applied', 'installing'].includes(s.state)) {
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
  const ignored = isIgnored(c)
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
        (old) => (old ? { ...old, action: 'none', ignored: false } : old))
    } catch (e) {
      setError(errText(e))
    }
  }

  const ignoreVersion = async () => {
    if (!c) return
    try {
      await apiPost('/api/system/update/ignore', { version: c.latest })
      qc.setQueryData<UpdateCheckOut>(QUERY_KEY,
        (old) => (old ? { ...old, action: 'none', ignored: true } : old))
      onClose()
    } catch (e) {
      setError(errText(e))
    }
  }

  // 取消忽略后必须真正重新 check：服务端会依据当前 manifest 重新判定，
  // 而不是只把前端文案改掉（否则用户仍看不到「立即更新」）。
  const unignoreVersion = async () => {
    setError('')
    setUnignoring(true)
    try {
      await apiPost('/api/system/update/unignore')
      qc.setQueryData<UpdateCheckOut>(QUERY_KEY,
        (old) => (old ? { ...old, ignored: false } : old))
      await qc.refetchQueries({ queryKey: QUERY_KEY })
    } catch (e) {
      setError(errText(e))
    } finally {
      setUnignoring(false)
    }
  }

  return (
    <Dialog open={open} onClose={downloading ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>系统更新</DialogTitle>
      <DialogContent dividers>
        {!update && !ignored && !applied && (
          <Alert severity="info" sx={{ mb: 2 }}>
            当前已经是最新版本（v{currentVersion(c)}）
          </Alert>
        )}
        {c?.retry && !applied && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            {state === 'done'
              ? '上次全量安装尚未生效，安装包已在本机，直接点「安装并重启」即可重试，无需重新下载。'
              : '上次全量安装尚未生效，已保留更新入口，可重新下载安装。'}
          </Alert>
        )}
        {ignored && !applied && (
          <Alert severity="info" sx={{ mb: 2 }}>
            已忽略此版本更新（v{c?.latest}）。厂商以更高发布序号重发同版本时会自动提醒，
            也可以点「取消忽略并重新检查」立即恢复。
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
        {state === 'installing' && (
          <Alert severity="info" sx={{ mt: 2 }}>
            全量安装已启动，正在等待新版本启动确认…
          </Alert>
        )}
        {applied && (
          <Alert severity="success" sx={{ mt: 2 }}>
            {status?.package_type === 'full'
              ? '全量安装已启动，程序将自动重启；若长时间没有重启，请重新打开本窗口检查更新并重试。'
              : '新版本已安装。请退出托盘程序后重新启动，即可使用新版本。'}
          </Alert>
        )}
        {error && !state.startsWith('error') && (
          <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={downloading || unignoring}>
          {applied ? '关闭' : '稍后'}
        </Button>
        {ignored && !applied && (
          <Button
            onClick={() => void unignoreVersion()}
            disabled={unignoring}
          >
            {unignoring ? '正在取消…' : '取消忽略并重新检查'}
          </Button>
        )}
        {update && c?.action !== 'forced' && !applied && !downloading && state !== 'done' && (
          <Button onClick={() => void ignoreVersion()} color="inherit">
            忽略此版本
          </Button>
        )}
        {state === 'installing' && (
          <Button onClick={() => void qc.refetchQueries({ queryKey: QUERY_KEY })}>
            重新检查
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
  const ignored = isIgnored(c)
  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="subtitle1" fontWeight={600}>系统更新</Typography>
        <Chip size="small" label={`当前版本 v${currentVersion(c)}`} />
        <Box sx={{ flexGrow: 1 }} />
        <Button
          size="small"
          onClick={() => void q.refetch()}
          disabled={q.isFetching}
        >
          {q.isFetching ? '检查中…' : '检查更新'}
        </Button>
        {(hasUpdate(c) || ignored) && (
          <Button size="small" variant="contained" onClick={() => setOpen(true)}>
            {ignored ? '更新详情' : '查看更新'}
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
                : ignored
                  ? `已忽略此版本更新（v${c.latest}），可在「更新详情」中取消忽略`
                  : `当前已经是最新版本（v${currentVersion(c)}）`}
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

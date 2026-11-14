import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  Paper,
  Snackbar,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import CloseIcon from '@mui/icons-material/Close'
import KeyIcon from '@mui/icons-material/VpnKey'
import { ApiError, apiGet, apiPost } from '../api/client'
import type { LicenseStatusOut } from '../api/types'

/**
 * 电脑授权界面（激活遮罩 + 顶部状态 Chip + 激活对话框）。
 *
 * 三种呈现：
 * - `ok=false`：全屏遮罩，不能关闭（未激活/已到期/授权无效/离线超期）；
 *   遮罩由 App 渲染在内容区之上，业务接口此时已被后端拦截（403）。
 * - `ok=true` 但离线/临近到期：顶部 Alert 横幅提示，不阻塞操作。
 * - 用户主动点顶部 Chip：打开对话框查看详情 / 换激活码 / 刷新。
 *
 * 授权未启用（`enabled=false`）时整个组件不渲染任何东西（开发/自用模式）。
 */

/** 需要用户立即处理的状态（对应后端 state 机） */
const BLOCKING_STATES = new Set([
  'unlicensed',
  'expired',
  'offline_expired',
  'invalid',
  'revoked',
  'clock_rollback',
])

const STATE_TITLES: Record<string, string> = {
  unlicensed: '需要激活才能使用',
  expired: '授权已到期',
  offline_expired: '离线超时，需要联网确认',
  invalid: '许可证无效',
  revoked: '授权已被吊销',
  clock_rollback: '系统时间异常',
}

/** 复制文本（http 非 localhost 下 navigator.clipboard 不可用时退化为 execCommand） */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 继续走兜底
  }
  try {
    const el = document.createElement('textarea')
    el.value = text
    el.style.position = 'fixed'
    el.style.opacity = '0'
    document.body.appendChild(el)
    el.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(el)
    return ok
  } catch {
    return false
  }
}

function formatDate(iso: string): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 顶部 Chip 的展示（颜色 + 文案） */
function chipView(status?: LicenseStatusOut) {
  if (!status) return null
  // 测试模式必须显眼：交付件忘了关，一眼就能看见
  if (status.dev_mode) return { label: '测试模式', color: 'error' as const }
  if (!status.enabled) return null
  if (!status.ok) {
    return { label: '未授权', color: 'error' as const }
  }
  if (status.state === 'offline_grace') {
    return { label: '离线授权', color: 'warning' as const }
  }
  const days = status.days_left
  if (days !== null && days <= 7) {
    return { label: `授权剩余 ${days} 天`, color: 'warning' as const }
  }
  if (days !== null) {
    return { label: `授权剩余 ${days} 天`, color: 'success' as const }
  }
  return { label: '已授权', color: 'success' as const }
}

export function useLicenseStatus() {
  return useQuery({
    queryKey: ['license'],
    queryFn: () => apiGet<LicenseStatusOut>('/api/license/status'),
    // 30 秒一轮：到期/吊销能在半分钟内反映到界面
    refetchInterval: 30000,
    retry: false,
  })
}

/** 顶部授权状态 Chip（点击打开对话框） */
export function LicenseChip(props: { status?: LicenseStatusOut; onClick: () => void }) {
  const view = chipView(props.status)
  if (!view) return null
  return (
    <Tooltip title={`${props.status?.message ?? ''}（点击打开授权窗口）`}>
      <Chip
        size="small"
        icon={<KeyIcon sx={{ fontSize: 16 }} />}
        label={view.label}
        color={view.color}
        onClick={props.onClick}
        sx={{ mr: 1, fontWeight: 600, cursor: 'pointer' }}
      />
    </Tooltip>
  )
}

/** 授权对话框（查看状态、验证激活码、联网刷新） */
export function LicenseDialog(props: {
  open: boolean
  status?: LicenseStatusOut
  onClose: () => void
}) {
  const { open, status, onClose } = props
  const queryClient = useQueryClient()
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')

  useEffect(() => {
    if (open) setError('')
  }, [open])

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['license'] })
    void queryClient.invalidateQueries()
  }

  const activate = useMutation({
    mutationFn: (activationCode: string) =>
      apiPost<LicenseStatusOut>('/api/license/activate', {
        activation_code: activationCode,
      }),
    onSuccess: (data) => {
      setError('')
      setCode('')
      setToast(`激活成功，到期时间 ${formatDate(data.expires_at)}`)
      refreshAll()
      if (data.ok) onClose()
    },
    onError: (err: unknown) => {
      setError(err instanceof ApiError ? err.message : '激活失败，请重试')
    },
  })

  const refresh = useMutation({
    mutationFn: () => apiPost<LicenseStatusOut>('/api/license/refresh'),
    onSuccess: (data) => {
      setError('')
      setToast(data.ok ? '已与授权服务器确认' : data.message)
      refreshAll()
    },
    onError: (err: unknown) => {
      setError(err instanceof ApiError ? err.message : '联网确认失败')
    },
  })

  const busy = activate.isPending || refresh.isPending

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <KeyIcon color="primary" />
        {STATE_TITLES[status?.state ?? ''] ?? '授权信息'}
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          {status && !status.ok && (
            <Alert severity="error">{status.message}</Alert>
          )}
          {status?.ok && status.state === 'offline_grace' && (
            <Alert severity="warning">
              暂时连不上授权服务器，当前处于离线宽限期（还剩 {status.grace_days} 天
              缓冲）。请检查网络，或点下方「刷新授权」重试。
            </Alert>
          )}
          {status?.ok && (status.days_left ?? 999) <= 7 && (
            <Alert severity="warning">
              授权将在 {formatDate(status.expires_at)}（{status.days_left} 天后）
              到期，请及时续费。
            </Alert>
          )}
          {status?.ok && status.last_error && (
            <Alert severity="warning">
              最近一次联网确认失败：{status.last_error}。
              当前仍以本地许可证为准（{status.grace_days} 天宽限内可用），
              请点「刷新授权」重试；若持续失败请联系供应商核对授权记录。
            </Alert>
          )}

          <Box>
            <Typography variant="body2" color="text.secondary" gutterBottom>
              本机机器码（把这一串发给供应商申请授权）
            </Typography>
            <Stack direction="row" spacing={1} alignItems="center">
              <TextField
                value={status?.machine_code ?? ''}
                fullWidth
                size="small"
                InputProps={{ readOnly: true }}
                sx={{ '& input': { fontFamily: 'monospace', letterSpacing: 0.5 } }}
              />
              <Tooltip title="复制机器码">
                <IconButton
                  onClick={async () => {
                    const ok = await copyText(status?.machine_code ?? '')
                    setToast(ok ? '机器码已复制' : '复制失败，请手动选中复制')
                  }}
                >
                  <ContentCopyIcon />
                </IconButton>
              </Tooltip>
            </Stack>
          </Box>

          <TextField
            label="激活码"
            placeholder="KF-XXXX-XXXX-XXXX-XXXX"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            size="small"
            fullWidth
            disabled={busy}
            helperText="没有激活码？先把上面的机器码发给供应商"
          />

          {error && <Alert severity="error">{error}</Alert>}

          <Divider />

          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            <Button
              variant="contained"
              disabled={busy || !code.trim()}
              onClick={() => activate.mutate(code.trim())}
            >
              {activate.isPending ? <CircularProgress size={18} /> : '激活'}
            </Button>
            <Button
              variant="outlined"
              disabled={busy || !status?.enabled}
              onClick={() => refresh.mutate()}
            >
              刷新授权
            </Button>
          </Stack>

          <Box sx={{ color: 'text.secondary', fontSize: 12, lineHeight: 1.9 }}>
            <div>授权服务器：{status?.server_url || '-'}</div>
            <div>许可证号：{status?.license_id || '-'}</div>
            <div>到期时间：{formatDate(status?.expires_at ?? '')}</div>
            <div>最近联网确认：{formatDate(status?.last_ok_at ?? '')}</div>
            {status?.customer ? <div>授权客户：{status.customer}</div> : null}
            <div>
              离线宽限：{status?.grace_days ?? 7} 天（断网期间可正常使用，
              超过后需联网确认一次）
            </div>
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>关闭</Button>
      </DialogActions>
      <Snackbar
        open={Boolean(toast)}
        autoHideDuration={3000}
        onClose={() => setToast('')}
        message={toast}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      />
    </Dialog>
  )
}

/**
 * 设置页「授权」标签页：随时查看机器码与授权状态。
 *
 * 与顶部 Chip 的区别：这里的入口**始终存在**（含 `enabled=false` 的自用模式），
 * 交付/售后时要客户报机器码，不必先激活一遍。
 */
export function LicenseSettingsTab() {
  const { data: status, isFetching, refetch } = useLicenseStatus()
  const [open, setOpen] = useState(false)
  const [toast, setToast] = useState('')
  const enabled = Boolean(status?.enabled)
  const ok = Boolean(status?.ok)
  const devMode = Boolean(status?.dev_mode)
  const nearExpiry = (status?.days_left ?? 999) <= 7

  // 「刷新状态」必须真的连一次服务器（心搏），而不是只重读本地缓存——
  // 吊销/续期/改期限都发生在服务器上，本地文件感知不到（现场踩过：
  // 后台吊销后客户端一直显示正常，因为按钮只刷了本地）。
  // 后台周期心搏默认 6 小时一次，手动点按钮就是为了让用户立刻看到结果。
  const serverRefresh = useMutation({
    mutationFn: () => apiPost<LicenseStatusOut>('/api/license/refresh'),
    onSuccess: (next) => {
      void refetch()
      setToast(next.ok
        ? `已联网确认：${next.state === 'offline_grace' ? '离线宽限中' : '授权正常'}`
        : `服务器返回：${STATE_TITLES[next.state] ?? next.state}`)
    },
    onError: (e) => setToast(`联网确认失败：${(e as Error).message}`),
  })

  const severity = devMode ? 'error'
    : ok ? (nearExpiry ? 'warning' : 'success') : 'error'
  let headline: string
  if (devMode) {
    headline = '厂商测试模式：授权校验已关闭'
      + '（交付客户前务必执行 python run.py --license-on）'
  } else if (!enabled) {
    headline = '当前未启用授权校验'
  } else if (ok) {
    headline = status?.state === 'offline_grace'
      ? '离线运行中（宽限期内可正常使用）'
      : `已授权，到期 ${formatDate(status?.expires_at ?? '')}`
  } else {
    headline = STATE_TITLES[status?.state ?? ''] ?? '需要激活'
  }

  return (
    <Stack spacing={2} sx={{ maxWidth: 760 }}>
      <Alert severity={severity}>{headline}</Alert>
      {status?.message && (enabled || devMode) && (
        <Typography variant="body2" color="text.secondary">
          {status.message}
        </Typography>
      )}
      {enabled && status?.ok && status.last_error && (
        <Alert severity="warning">
          最近一次联网确认失败：{status.last_error}
        </Alert>
      )}

      <Box>
        <Typography variant="subtitle2" gutterBottom>
          本机机器码
        </Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          <TextField
            value={status?.machine_code ?? ''}
            fullWidth
            size="small"
            InputProps={{ readOnly: true }}
            sx={{ '& input': { fontFamily: 'monospace', letterSpacing: 0.5 } }}
          />
          <Tooltip title="复制机器码">
            <IconButton
              onClick={async () => {
                const done = await copyText(status?.machine_code ?? '')
                setToast(done ? '机器码已复制' : '复制失败，请手动选中复制')
              }}
            >
              <ContentCopyIcon />
            </IconButton>
          </Tooltip>
        </Stack>
        <Typography variant="caption" color="text.secondary">
          把这串发给供应商申请授权。机器码由本机硬件派生，重装系统或更换主板
          后会变化（需要重新授权）。
        </Typography>
      </Box>

      <Divider />

      <Stack direction="row" spacing={3} flexWrap="wrap" useFlexGap>
        <Box>
          <Typography variant="caption" color="text.secondary">
            授权状态
          </Typography>
          <Typography variant="body2">
            {enabled ? (status?.state ?? '-') : '测试模式（未校验）'}
          </Typography>
        </Box>
        <Box>
          <Typography variant="caption" color="text.secondary">
            到期时间
          </Typography>
          <Typography variant="body2">
            {formatDate(status?.expires_at ?? '')}
          </Typography>
        </Box>
        <Box>
          <Typography variant="caption" color="text.secondary">
            剩余天数
          </Typography>
          <Typography variant="body2">
            {status?.days_left ?? '-'}
          </Typography>
        </Box>
        <Box>
          <Typography variant="caption" color="text.secondary">
            许可证号
          </Typography>
          <Typography variant="body2">{status?.license_id || '-'}</Typography>
        </Box>
        <Box>
          <Typography variant="caption" color="text.secondary">
            最近联网确认
          </Typography>
          <Typography variant="body2">
            {formatDate(status?.last_ok_at ?? '')}
          </Typography>
        </Box>
      </Stack>

      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
        <Button variant="contained" onClick={() => setOpen(true)}>
          {ok ? '验证授权码' : '输入激活码'}
        </Button>
        <Button
          variant="outlined"
          disabled={!enabled || serverRefresh.isPending}
          onClick={() => serverRefresh.mutate()}
        >
          {serverRefresh.isPending ? '联网确认中…' : '刷新状态'}
        </Button>
      </Stack>

      {!enabled && (
        <Box sx={{ color: 'text.secondary', fontSize: 12, lineHeight: 1.9 }}>
          <div>本版本<b>默认强制授权</b>：把 <code>license.enabled</code> 改成
            {' '}<code>0</code> / <code>false</code> / 留空都<b>不会</b>关闭校验，
            只有厂商测试口令才生效（未激活时才会看到这段说明）。
          </div>
          <div style={{ marginTop: 6 }}>
            <b>切回强制授权</b>：<code>python run.py --license-on</code>
            （或把 <code>license.enabled</code> 写成 <code>1</code>，重启生效）。
          </div>
          <div>
            <b>交付客户</b>：启动脚本加 <code>--license</code>（环境变量
            {' '}<code>KEFU_LICENSE_MODE=enforce</code>），这样连测试口令也无效。
          </div>
        </Box>
      )}

      <LicenseDialog
        open={open}
        status={status}
        onClose={() => {
          setOpen(false)
          void refetch()
        }}
      />
      <Snackbar
        open={Boolean(toast)}
        autoHideDuration={3000}
        onClose={() => setToast('')}
        message={toast}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      />
    </Stack>
  )
}

/** 当日不再提示的 localStorage 键（值为 `${日期}|${提示身份}`） */
const NOTICE_DISMISS_KEY = 'license.notice.dismissed'

/** 本地日期 YYYY-MM-DD（不用 toISOString——那是 UTC，会提前一天翻日）。 */
function localToday(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function readNoticeDismissed(): string {
  try {
    return localStorage.getItem(NOTICE_DISMISS_KEY) ?? ''
  } catch {
    return ''           // 隐私模式等存不进去：退化为「每次都提示」
  }
}

function writeNoticeDismissed(value: string): void {
  try {
    localStorage.setItem(NOTICE_DISMISS_KEY, value)
  } catch {
    /* 存不进去就算了，下次还会提示，无伤大雅 */
  }
}

/**
 * 全局授权门：横幅提示 + 不可关闭的激活遮罩。
 *
 * 到期前 7 天起每天提示剩余天数；用户点 × 关闭后**当日**不再提示，
 * 次日（或剩余天数变化，比如续费后又到期）自动恢复提示。
 *
 * Args:
 *     onOpen: 「查看」按钮/顶部 Chip 被点击时通知外层打开对话框。
 */
export function LicenseNotice(props: {
  status?: LicenseStatusOut
  onOpen: () => void
}) {
  const { status } = props
  const [dismissedMark, setDismissedMark] = useState(readNoticeDismissed)
  if (!status || !status.enabled || !status.ok) return null
  // 只提示「离线」与「7 天内到期」两种，正常状态不打扰
  const nearExpiry = (status.days_left ?? 999) <= 7
  if (status.state !== 'offline_grace' && !nearExpiry) return null
  // 提示身份 = 状态 + 剩余天数：跨天必换日期、续费/到期必变剩余天数，
  // 两者任一变化都会让关闭记录失效、横幅重新出现
  const noticeKey = `${status.state}:${status.days_left}`
  if (dismissedMark === `${localToday()}|${noticeKey}`) return null
  const text =
    status.state === 'offline_grace'
      ? `离线运行中：暂时连不上授权服务器（宽限 ${status.grace_days} 天内可继续使用）`
      : `授权将在 ${formatDate(status.expires_at)} 到期（剩余 ${status.days_left} 天），请及时续费`
  const dismiss = () => {
    const mark = `${localToday()}|${noticeKey}`
    writeNoticeDismissed(mark)
    setDismissedMark(mark)
  }
  return (
    <Alert
      severity="warning"
      sx={{ mb: 2 }}
      action={
        <Stack direction="row" spacing={0.5} alignItems="center">
          <Button color="inherit" size="small" onClick={props.onOpen}>
            查看
          </Button>
          <IconButton color="inherit" size="small" onClick={dismiss}
                      aria-label="今日不再提示">
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
      }
    >
      {text}
    </Alert>
  )
}

/** 未授权时的全屏遮罩（挡住业务界面，避免用户对着 403 发呆） */
export function LicenseBlocker(props: {
  status?: LicenseStatusOut
  onOpen: () => void
}) {
  const { status } = props
  if (!status || !status.enabled || status.ok) return null
  return (
    <Box
      sx={{
        position: 'fixed',
        inset: 0,
        // 必须低于 AppBar（zIndex.drawer + 1）：否则顶栏的授权 Chip 会被盖住，
        // 用户连点开激活框的入口都没了。Dialog 的 zIndex.modal 仍在它之上。
        zIndex: (t) => t.zIndex.drawer,
        bgcolor: 'rgba(17, 24, 28, 0.72)',
        backdropFilter: 'blur(2px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        p: 2,
      }}
    >
      <Paper sx={{ p: 4, maxWidth: 460, textAlign: 'center' }}>
        <KeyIcon color="warning" sx={{ fontSize: 42 }} />
        <Typography variant="h6" sx={{ mt: 1 }}>
          {STATE_TITLES[status.state] ?? '需要激活'}
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mt: 1, mb: 2 }}>
          {status.message}
        </Typography>
        <Typography variant="body2" sx={{ mb: 1 }}>
          本机机器码
        </Typography>
        <Typography
          sx={{ fontFamily: 'monospace', fontSize: 13, mb: 2, wordBreak: 'break-all' }}
        >
          {status.machine_code}
        </Typography>
        <Button variant="contained" onClick={props.onOpen} sx={{ mb: 1 }}>
          输入激活码
        </Button>
        <Typography variant="caption" color="text.secondary" display="block">
          也可以点右上角「未授权」按钮；历史数据未受影响，激活后原样恢复
        </Typography>
      </Paper>
    </Box>
  )
}

/** 判断当前状态是否需要遮罩/强制打开对话框 */
export function isBlocking(status?: LicenseStatusOut): boolean {
  if (!status || !status.enabled) return false
  return !status.ok || BLOCKING_STATES.has(status.state)
}

import { useEffect, useState } from 'react'
import { NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  AppBar,
  Badge,
  Box,
  Drawer,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Snackbar,
  Toolbar,
  Typography,
} from '@mui/material'
import Inventory2Icon from '@mui/icons-material/Inventory2'
import ChatIcon from '@mui/icons-material/Chat'
import ReceiptLongIcon from '@mui/icons-material/ReceiptLong'
import SettingsIcon from '@mui/icons-material/Settings'
import { apiGet } from './api/client'
import type { StatusOut } from './api/types'
import { useEvents } from './hooks/useEvents'
import { armAudioUnlock, playOpportunitySound } from './hooks/sound'
import { setPendingOpportunity } from './hooks/pendingOpportunity'
import OpportunityList from './pages/OpportunityList'
import MessageBrowse from './pages/MessageBrowse'
import AnalysisLogs from './pages/AnalysisLogs'
import SettingsPage from './pages/SettingsPage'

const DRAWER_WIDTH = 200

/** 新商机 toast 数据（useEvents 派发 kefu-opportunity-toast） */
interface OppToastData {
  id: number
  session_name: string
  summary: string
}

/** AI 分析失败提示数据（useEvents 派发 kefu-analysis-error） */
interface AnalysisErrorData {
  error: string
  consecutive_failures: number
}

/** 全局框架：左侧导航、WS 事件接入、顶部状态横幅、全局提醒。 */
export default function App() {
  const navigate = useNavigate()
  useEvents() // WS 订阅 + react-query 失效联动

  // 状态轮询：WS 断开时的兜底刷新通道
  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: () => apiGet<StatusOut>('/api/status'),
    refetchInterval: 5000,
  })

  // 全局提示音（设置页保存后写入 localStorage；opportunity_new 时由 useEvents 派发事件）。
  // 提示音经 Web Audio 合成；首次交互解锁以绕过浏览器自动播放策略。
  // 开关状态响应式：storage 事件（跨页签）+ kefu-sound-settings-changed
  // 自定义事件（同页保存，storage 事件同页不触发），无需刷新即生效。
  const [soundEnabled, setSoundEnabled] = useState(
    () => window.localStorage.getItem('kefu.sound_enabled') === '1',
  )
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === 'kefu.sound_enabled') {
        setSoundEnabled(window.localStorage.getItem('kefu.sound_enabled') === '1')
      }
    }
    const onSoundChanged = () => {
      setSoundEnabled(window.localStorage.getItem('kefu.sound_enabled') === '1')
    }
    window.addEventListener('storage', onStorage)
    window.addEventListener('kefu-sound-settings-changed', onSoundChanged)
    return () => {
      window.removeEventListener('storage', onStorage)
      window.removeEventListener('kefu-sound-settings-changed', onSoundChanged)
    }
  }, [])
  useEffect(() => {
    if (!soundEnabled) return
    armAudioUnlock() // 页面加载即武装解锁监听，首次交互后恢复播放能力
    const handler = () => playOpportunitySound()
    window.addEventListener('kefu-opportunity-new-sound', handler)
    return () => window.removeEventListener('kefu-opportunity-new-sound', handler)
  }, [soundEnabled])

  // 新商机到达提醒（右上角 toast，点击跳转商机详情；全局任何页面生效）
  const [oppToast, setOppToast] = useState<{
    id: number
    content: string
  } | null>(null)
  useEffect(() => {
    const handler = (ev: Event) => {
      const d = (ev as CustomEvent<OppToastData>).detail
      const firstLine =
        String(d.summary ?? '')
          .split('\n')
          .map((l) => l.trim())
          .find(Boolean) ?? ''
      setOppToast({
        id: d.id,
        content: [d.session_name, firstLine].filter(Boolean).join('：'),
      })
    }
    window.addEventListener('kefu-opportunity-toast', handler)
    return () => window.removeEventListener('kefu-opportunity-toast', handler)
  }, [])

  // AI 分析失败提醒（analysis_error WS 事件；连续失败 ≥3 次转常驻横幅）
  const [aiErrorSnack, setAiErrorSnack] = useState<string | null>(null)
  useEffect(() => {
    const handler = (ev: Event) => {
      const d = (ev as CustomEvent<AnalysisErrorData>).detail
      setAiErrorSnack(
        `AI 分析失败：${d.error}（已连续 ${d.consecutive_failures} 次），请检查 AI 接口配置与额度`,
      )
    }
    window.addEventListener('kefu-analysis-error', handler)
    return () => window.removeEventListener('kefu-analysis-error', handler)
  }, [])

  // 点击 toast：回到商机列表页并打开对应详情抽屉（OpportunityList 监听该事件）。
  // 列表页未挂载时事件会丢失：用模块级 pending 暂存，挂载后消费打开抽屉。
  const openOpportunity = (id: number) => {
    setOppToast(null)
    setPendingOpportunity(id)
    navigate('/')
    window.dispatchEvent(new CustomEvent('kefu-open-opportunity', { detail: { id } }))
  }

  // 系统通知点击（页面不可见时收到新商机 → 点击通知回焦并打开详情抽屉）
  useEffect(() => {
    const handler = (ev: Event) => {
      const d = (ev as CustomEvent<{ id: number }>).detail
      if (typeof d?.id === 'number') openOpportunity(d.id)
    }
    window.addEventListener('kefu-notification-click', handler)
    return () => window.removeEventListener('kefu-notification-click', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const banner = buildBanner(status)

  return (
    <Box sx={{ display: 'flex', minHeight: '100vh' }}>
      <AppBar
        position="fixed"
        elevation={0}
        sx={{ zIndex: (t) => t.zIndex.drawer + 1, bgcolor: '#2e2e2e' }}
      >
        <Toolbar variant="dense">
          <Typography variant="subtitle1" noWrap sx={{ flexGrow: 1 }}>
            微信商机监测与客服系统
          </Typography>
          <Typography variant="caption" sx={{ mr: 2 }}>
            {status?.listener_running
              ? `监听运行中 · 监控 ${status.monitored_count} 个会话`
              : '监听未运行'}
          </Typography>
          <Typography variant="caption" sx={{ color: status?.wechat.visible ? '#07c160' : '#e0a800' }}>
            {status?.wechat.visible ? '微信窗口可用' : '微信窗口不可用'}
          </Typography>
        </Toolbar>
      </AppBar>

      <Drawer
        variant="permanent"
        sx={{
          width: DRAWER_WIDTH,
          flexShrink: 0,
          '& .MuiDrawer-paper': { width: DRAWER_WIDTH, boxSizing: 'border-box' },
        }}
      >
        <Toolbar variant="dense" />
        <List>
          <NavItem
            to="/"
            icon={<Inventory2Icon />}
            label="商机列表"
            badge={status?.unread_count ?? 0}
          />
          <NavItem to="/messages" icon={<ChatIcon />} label="消息浏览" />
          <NavItem to="/analysis-logs" icon={<ReceiptLongIcon />} label="分析日志" />
          <NavItem to="/settings" icon={<SettingsIcon />} label="设置" />
        </List>
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, p: 2, mt: 5 }}>
        {banner && (
          <Alert severity={banner.severity} sx={{ mb: 2 }}>
            {banner.text}
          </Alert>
        )}
        <Routes>
          <Route path="/" element={<OpportunityList />} />
          <Route path="/messages" element={<MessageBrowse />} />
          <Route path="/analysis-logs" element={<AnalysisLogs />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<OpportunityList />} />
        </Routes>
      </Box>

      {/* 新商机到达提醒（右上角，6 秒自动消失，可手动关闭，点击跳详情） */}
      <Snackbar
        open={oppToast !== null}
        autoHideDuration={6000}
        onClose={() => setOppToast(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        <Alert
          severity="success"
          onClose={() => setOppToast(null)}
          sx={{
            width: '100%',
            minWidth: 300,
            cursor: 'pointer',
            alignItems: 'flex-start',
          }}
          onClick={() => oppToast && openOpportunity(oppToast.id)}
        >
          <Typography variant="subtitle2" sx={{ fontWeight: 600 }}>
            发现新商机
          </Typography>
          <Typography variant="body2">{oppToast?.content}</Typography>
        </Alert>
      </Snackbar>

      {/* AI 分析失败警告（右上角，8 秒自动消失；连续失败 ≥3 次由顶部横幅常驻提示） */}
      <Snackbar
        open={aiErrorSnack !== null}
        autoHideDuration={8000}
        onClose={() => setAiErrorSnack(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        <Alert
          severity="warning"
          onClose={() => setAiErrorSnack(null)}
          sx={{ width: '100%', maxWidth: 420 }}
        >
          {aiErrorSnack}
        </Alert>
      </Snackbar>
    </Box>
  )
}

function NavItem(props: {
  to: string
  icon: React.ReactNode
  label: string
  badge?: number
}) {
  return (
    <ListItemButton component={NavLink} to={props.to}>
      <ListItemIcon sx={{ minWidth: 36 }}>{props.icon}</ListItemIcon>
      <ListItemText primary={props.label} />
      {(props.badge ?? 0) > 0 && (
        <Badge badgeContent={props.badge} color="error" />
      )}
    </ListItemButton>
  )
}

function buildBanner(status?: StatusOut) {
  if (!status) return null
  // AI 分析连续失败 ≥3 次：常驻横幅（恢复成功后 analysis_health 归零自动消失）
  const failures = status.analysis_health?.consecutive_failures ?? 0
  if (failures >= 3) {
    const lastError = status.analysis_health?.last_error ?? ''
    return {
      severity: 'error' as const,
      text: `AI 分析连续失败 ${failures} 次${lastError ? `：${lastError}` : ''}，请检查 AI 接口配置与额度`,
    }
  }
  if (status.degraded_reason) {
    return { severity: 'warning' as const, text: `微信接入降级：${status.degraded_reason}` }
  }
  if (!status.wechat.alive) {
    return { severity: 'warning' as const, text: '微信未登录或数据库不可读，商机监听未启动' }
  }
  if (!status.wechat.visible) {
    return { severity: 'info' as const, text: '发送功能暂不可用（微信窗口被遮挡或锁屏），商机监听不受影响' }
  }
  if (!status.ai_configured) {
    return { severity: 'info' as const, text: 'AI 接口尚未配置完整，请在「设置 → AI 接口」完成配置' }
  }
  return null
}

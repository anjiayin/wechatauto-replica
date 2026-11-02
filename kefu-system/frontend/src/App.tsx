import { useEffect, useMemo } from 'react'
import { NavLink, Route, Routes } from 'react-router-dom'
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
  Toolbar,
  Typography,
} from '@mui/material'
import Inventory2Icon from '@mui/icons-material/Inventory2'
import ChatIcon from '@mui/icons-material/Chat'
import SettingsIcon from '@mui/icons-material/Settings'
import { apiGet } from './api/client'
import type { StatusOut } from './api/types'
import { useEvents } from './hooks/useEvents'
import OpportunityList from './pages/OpportunityList'
import MessageBrowse from './pages/MessageBrowse'
import SettingsPage from './pages/SettingsPage'

const DRAWER_WIDTH = 200

/** 全局框架：左侧导航、WS 事件接入、顶部状态横幅。 */
export default function App() {
  useEvents() // WS 订阅 + react-query 失效联动

  // 状态轮询：WS 断开时的兜底刷新通道
  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: () => apiGet<StatusOut>('/api/status'),
    refetchInterval: 5000,
  })

  // 全局提示音（设置页保存后写入 localStorage；opportunity_new 时由 useEvents 派发事件）
  const soundEnabled = useMemo(
    () => window.localStorage.getItem('kefu.sound_enabled') === '1',
    [],
  )
  useEffect(() => {
    if (!soundEnabled) return
    const handler = () => {
      try {
        const ctx = new AudioContext()
        const osc = ctx.createOscillator()
        const gain = ctx.createGain()
        osc.connect(gain)
        gain.connect(ctx.destination)
        osc.frequency.value = 880
        gain.gain.setValueAtTime(0.15, ctx.currentTime)
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5)
        osc.start()
        osc.stop(ctx.currentTime + 0.5)
      } catch {
        // 忽略音频设备异常
      }
    }
    window.addEventListener('kefu-opportunity-new-sound', handler)
    return () => window.removeEventListener('kefu-opportunity-new-sound', handler)
  }, [soundEnabled])

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
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<OpportunityList />} />
        </Routes>
      </Box>
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

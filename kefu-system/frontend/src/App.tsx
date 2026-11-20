import { useEffect, useRef, useState } from 'react'
import { NavLink, Route, Routes, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Alert,
  AppBar,
  Badge,
  Box,
  Chip,
  Drawer,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Snackbar,
  Toolbar,
  Tooltip,
  Typography,
} from '@mui/material'
import Inventory2Icon from '@mui/icons-material/Inventory2'
import ReceiptLongIcon from '@mui/icons-material/ReceiptLong'
import SummarizeIcon from '@mui/icons-material/Summarize'
import SettingsIcon from '@mui/icons-material/Settings'
import { apiGet } from './api/client'
import type { StatusOut } from './api/types'
import { useEvents } from './hooks/useEvents'
import { armAudioUnlock, playOpportunitySound } from './hooks/sound'
import { setPendingOpportunity } from './hooks/pendingOpportunity'
import OpportunityList from './pages/OpportunityList'
import AnalysisLogs from './pages/AnalysisLogs'
import GroupSummary from './pages/GroupSummary'
import SettingsPage from './pages/SettingsPage'
import { UpdateChip, UpdateDialog, useUpdateCheck } from './pages/UpdatePanel'
import {
  LicenseBlocker,
  LicenseChip,
  LicenseDialog,
  LicenseNotice,
  useLicenseStatus,
} from './pages/LicensePanel'

const DRAWER_WIDTH = 200

/** 新商机 toast 数据（useEvents 派发 kefu-opportunity-toast） */
interface OppToastData {
  id: number
  /** 提醒标题（随商机类型变化，如「收到新的客户投诉」） */
  title?: string
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

  // 客户端更新：启动 5s 后检查一次、之后每 12h；发现新版本自动提示一次。
  const updateCheck = useUpdateCheck(true)
  const [updateOpen, setUpdateOpen] = useState(false)
  const updateAutoOpened = useRef(false)
  useEffect(() => {
    const u = updateCheck.data
    if (u && (u.action === 'optional' || u.action === 'forced')
        && !updateAutoOpened.current) {
      updateAutoOpened.current = true
      setUpdateOpen(true)
    }
  }, [updateCheck.data])

  // 授权状态（未启用授权时 enabled=false，界面完全不打扰）
  const { data: license } = useLicenseStatus()
  const [licenseOpen, setLicenseOpen] = useState(false)
  const licenseAutoOpened = useRef(false)
  useEffect(() => {
    // 未授权/过期时自动弹一次激活框（用户关掉后回到遮罩，仍可从顶栏再打开）
    if (license?.enabled && !license.ok && !licenseAutoOpened.current) {
      licenseAutoOpened.current = true
      setLicenseOpen(true)
    }
  }, [license])

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
    title: string
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
        title: d.title || '收到新商机',
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
        sx={{
          zIndex: (t) => t.zIndex.drawer + 1,
          // 深色顶栏 + 绿色下边框：与下方白色内容区明确分层（反馈 4）
          bgcolor: '#24292b',
          borderBottom: '2px solid',
          borderColor: 'primary.main',
        }}
      >
        <Toolbar variant="dense">
          <Typography variant="subtitle1" noWrap sx={{ flexGrow: 1, fontWeight: 600 }}>
            微信商机监测与客服系统
          </Typography>
          <Typography variant="caption" sx={{ mr: 2, color: 'rgba(255,255,255,0.85)' }}>
            {status?.listener_running
              ? `监听运行中 · 监控 ${status.monitored_count} 个会话`
              : '监听未运行'}
          </Typography>
          {/* 客户端版本 / 新版本提示（点击查看更新详情） */}
          <UpdateChip check={updateCheck.data} onClick={() => setUpdateOpen(true)} />
          {/* 授权状态（点击查看机器码/激活码/到期时间） */}
          <LicenseChip status={license} onClick={() => setLicenseOpen(true)} />
          {/* 今日 AI 估算费用（反馈 13；未配置单价时为 0 不展示） */}
          {(status?.today_cost ?? 0) > 0 && (
            <Tooltip title="今日 AI 估算费用（按设置中的单价与实际 token 用量计算）">
              <Typography variant="caption" sx={{ mr: 2, color: 'rgba(255,255,255,0.85)' }}>
                今日 AI 费用：¥{(status!.today_cost).toFixed(2)}
              </Typography>
            </Tooltip>
          )}
          {/* 微信窗口状态：浅底深字高对比（深色顶栏上不再糊成一团，反馈 4） */}
          <Chip
            size="small"
            label={status?.wechat.visible ? '微信窗口可用' : '微信窗口不可用'}
            sx={{
              mr: 1,
              fontWeight: 600,
              color: status?.wechat.visible ? '#0b6b34' : '#a3231d',
              bgcolor: status?.wechat.visible ? '#e6f6ec' : '#fdecea',
              border: '1px solid',
              borderColor: status?.wechat.visible ? '#7fd3a0' : '#f0b4b0',
            }}
          />
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
          {/* 消息浏览菜单已按用户要求下线（反馈 8） */}
          {/* P1 定时产出数 Badge 位：T05 接入 WS summary_new 后填充 */}
          <NavItem to="/summary" icon={<SummarizeIcon />} label="群消息总结" />
          <NavItem to="/analysis-logs" icon={<ReceiptLongIcon />} label="分析日志" />
          <NavItem to="/settings" icon={<SettingsIcon />} label="设置" />
        </List>
      </Drawer>

      <Box component="main" sx={{ flexGrow: 1, p: 2, mt: 5 }}>
        {/* 授权提醒：离线宽限期 / 7 天内到期（不阻塞操作） */}
        <LicenseNotice status={license} onOpen={() => setLicenseOpen(true)} />
        {/* 强制授权已上线：这里只为「厂商自己的测试模式」留一个显眼提示，
            交付件里出现这条横幅 = 忘了执行 run.py --license-on */}
        {license?.dev_mode && (
          <Alert severity="error" sx={{ mb: 2 }}>
            当前是<b>厂商测试模式</b>：授权校验已关闭，本机无需激活即可使用。
            交付客户前请执行 <code>python run.py --license-on</code> 后重启。
          </Alert>
        )}
        {banner && (
          <Alert severity={banner.severity} sx={{ mb: 2 }}>
            {banner.text}
          </Alert>
        )}
        <Routes>
          <Route path="/" element={<OpportunityList />} />
          <Route path="/summary" element={<GroupSummary />} />
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
            {oppToast?.title ?? '收到新商机'}
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

      {/* 未授权 / 到期：全屏遮罩（业务接口此时已被后端拦截，数据不受影响） */}
      <LicenseBlocker status={license} onOpen={() => setLicenseOpen(true)} />

      {/* 授权详情/激活对话框 */}
      <LicenseDialog
        open={licenseOpen}
        status={license}
        onClose={() => setLicenseOpen(false)}
      />

      {/* 系统更新：确认关杀软 → 下载 → 安装（切换后需重启托盘程序） */}
      <UpdateDialog
        open={updateOpen}
        onClose={() => setUpdateOpen(false)}
        check={updateCheck.data}
      />
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
  // 图片解密密钥缺失：图片消息会一直取图失败、不进入商机分析。此前该提醒只在
  // 设置页「数据与账单」tab 内，用户实际在商机列表/分析日志页看不到，误以为
  // 「图片没抓取」。提升到全局横幅，任何页面都能看到该补救动作。
  // 注意：必须放在 AI/wechat 致命问题之后，避免盖过更关键的状态提示。
  if (status.image_key_ready === false) {
    return {
      severity: 'warning' as const,
      text: '图片解密密钥尚未获取：请在微信中打开任意一张图片查看大图，然后到「设置 → 数据与账单」点「重新检查」'
        + '主动捕获（约 2~4 分钟）；捕获成功后未识别的图片会自动补识别。',
    }
  }
  if (!status.wechat.visible) {
    return { severity: 'info' as const, text: '发送功能暂不可用（微信窗口被遮挡或锁屏），商机监听不受影响' }
  }
  // 关闭 AI 接口：商机由「轻量筛选」关键词/正则产生，无需提示去配置 AI
  if (status.ai_enabled === false) {
    return {
      severity: 'info' as const,
      text: '当前未使用 AI 接口：商机由「设置 → 分析判定 → 轻量筛选」的关键词/正则产生',
    }
  }
  if (!status.ai_configured) {
    return { severity: 'info' as const, text: 'AI 接口尚未配置完整，请在「设置 → AI 接口」完成配置' }
  }
  return null
}

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Checkbox,
  Chip,
  Divider,
  Drawer,
  FormControlLabel,
  IconButton,
  LinearProgress,
  Skeleton,
  Snackbar,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import RefreshIcon from '@mui/icons-material/Refresh'
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff'
import TaskAltIcon from '@mui/icons-material/TaskAlt'
import UndoIcon from '@mui/icons-material/Undo'
import SendIcon from '@mui/icons-material/Send'
import FormatQuoteIcon from '@mui/icons-material/FormatQuote'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import PauseIcon from '@mui/icons-material/Pause'
import ForwardToInboxIcon from '@mui/icons-material/ForwardToInbox'
import { apiGet, apiPatch, apiPost } from '../api/client'
import type {
  GroupMembers,
  ImageAnalysis,
  OpportunityContextOut,
  OpportunityDetail,
  ReplyPayload,
  ReplyTarget,
  WechatJumpResult,
} from '../api/types'

interface SendResultData {
  opportunity_id: number
  ok: boolean
  message: string
  /** 人工确认模式：内容只填进了输入框，还没真正发出 */
  manual?: boolean
  /** 发送链路分段耗时（毫秒）：init/front/open/send */
  stages?: Record<string, number>
  total_ms?: number
}

/** 回复去向：session=商机会话（群商机即群内回复）/ sender=私信发送人 */
type ReplyTargetKind = 'session' | 'sender'

const STAGE_LABELS: Record<string, string> = {
  init: '组件初始化',
  front: '前置窗口',
  open: '打开会话',
  send: '输入发送',
  fail: '失败前',
}

/** 分段耗时摘要（让用户一眼看出慢在「打开会话」还是「输入/确认」）。 */
function stageText(data: SendResultData): string {
  const stages = data.stages || {}
  const parts = Object.entries(stages)
    .filter(([k]) => k !== 'fail')
    .map(([k, v]) => `${STAGE_LABELS[k] ?? k} ${(v / 1000).toFixed(1)}s`)
  if (parts.length === 0) return ''
  return `${parts.join(' · ')} · 合计 ${(((data.total_ms ?? 0) / 1000)).toFixed(1)}s`
}

/**
 * 「群内回复默认 @ 发送人」的用户偏好（记住上次选择）。
 *
 * 存在 localStorage 而不是后端：这是每台电脑的操作习惯，换人用同一套系统
 * 时不该互相影响，也没必要为它加一列数据库字段。
 */
const AT_SENDER_KEY = 'kefu.reply.atSender'

function readAtSenderPref(): boolean {
  try {
    // 没有记录时默认勾选（群里回复先说「@张三」是常态）
    return window.localStorage.getItem(AT_SENDER_KEY) !== '0'
  } catch {
    return true // 隐私模式/存储被禁：用默认值，不影响回复
  }
}

function saveAtSenderPref(value: boolean): void {
  try {
    window.localStorage.setItem(AT_SENDER_KEY, value ? '1' : '0')
  } catch {
    /* 存不下就只是下次不记得，不该阻断回复 */
  }
}

/** 上下文中的图片消息：尝试展示解密真图，加载失败回退占位文本（反馈 2）。 */function ContextImage(props: { username: string; localId: number; fallback: string }) {
  const [failed, setFailed] = useState(false)
  // 图片识别结果（批次 6 需求 5）：是车辆/汽车配件就在图下方标出来。
  // 未识别过（404）或接口失败一律静默——图片很多，不能弹错误。
  const analysisQuery = useQuery({
    queryKey: ['image-analysis', props.username, props.localId],
    queryFn: () =>
      apiGet<ImageAnalysis>(
        `/api/messages/${encodeURIComponent(props.username)}/images/${props.localId}/analysis`,
      ),
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
  const analysis = analysisQuery.data
  if (failed) {
    return (
      <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
        {props.fallback || '[图片]'}
      </Typography>
    )
  }
  return (
    <Box>
      <Box
        component="img"
        src={`/api/messages/${encodeURIComponent(props.username)}/images/${props.localId}`}
        alt="图片消息"
        loading="lazy"
        onError={() => setFailed(true)}
        sx={{
          maxWidth: 240,
          maxHeight: 180,
          display: 'block',
          borderRadius: 1,
          border: '1px solid #e0e0e0',
          mt: 0.5,
        }}
      />
      {analysis?.is_vehicle_part && (
        <Stack direction="row" spacing={0.5} sx={{ mt: 0.5, flexWrap: 'wrap' }} useFlexGap>
          <Chip
            size="small"
            label={analysis.part_name ? `车辆/配件 · ${analysis.part_name}` : '车辆/配件'}
          />
          {analysis.vin && (
            <Chip size="small" variant="outlined" label={`VIN ${analysis.vin}`} />
          )}
        </Stack>
      )}
      {/* 车架号被主动丢弃时说明原因：否则用户看到「这次没有 VIN」会以为识别退化了 */}
      {analysis?.is_vehicle_part && !analysis.vin && analysis.error && (
        <Typography variant="caption" display="block" color="text.secondary" sx={{ mt: 0.5 }}>
          {analysis.error}
        </Typography>
      )}
    </Box>
  )
}

/** 上下文中的语音消息：播放/暂停按钮（HTML5 audio）+ 转写文本（反馈 ⑧）。 */
function VoicePlayer(props: { username: string; localId: number; text: string }) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState(false)
  const [failed, setFailed] = useState(false)
  // 卸载时停掉音频，避免抽屉关闭后还在出声
  useEffect(() => () => {
    audioRef.current?.pause()
    audioRef.current = null
  }, [])
  const toggle = () => {
    if (playing) {
      audioRef.current?.pause()
      setPlaying(false)
      return
    }
    if (!audioRef.current) {
      const audio = new Audio(
        `/api/messages/${encodeURIComponent(props.username)}/voice/${props.localId}`,
      )
      audio.onended = () => setPlaying(false)
      audio.onpause = () => setPlaying(false)
      audio.onerror = () => {
        setPlaying(false)
        setFailed(true)
      }
      audioRef.current = audio
    }
    audioRef.current
      .play()
      .then(() => setPlaying(true))
      .catch(() => {
        setPlaying(false)
        setFailed(true)
      })
  }
  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={0.5}>
        <Tooltip title={failed ? '语音播放失败（可看下方转写文本）' : playing ? '暂停' : '播放语音'}>
          <span>
            <IconButton
              size="small"
              color={playing ? 'primary' : 'default'}
              disabled={failed}
              onClick={toggle}
            >
              {playing ? <PauseIcon fontSize="small" /> : <PlayArrowIcon fontSize="small" />}
            </IconButton>
          </span>
        </Tooltip>
        <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
          {props.text || '[语音]'}
        </Typography>
      </Stack>
      {/* 播放失败要「看得见」：只有 tooltip 的话用户不知道点了为什么没声音（批次 6 需求 4） */}
      {failed && (
        <Typography variant="caption" color="error" sx={{ display: 'block', pl: 0.5 }}>
          语音加载失败，仅显示转写文字
        </Typography>
      )}
    </Box>
  )
}

/** 原文行 → 语音纯文本（去时间前缀 / 群发送人前缀 / 语音标记）；非语音行返回空串。 */
function voiceLineText(line: string): string {
  if (!line.includes('[语音]')) return ''
  let body = line.replace(/^\[\d{1,2}:\d{2}\]\s*/, '')
  // 群消息行形如「张三: [语音] 文本」：仅当冒号后紧跟语音标记时才剥发送人
  const withSender = body.match(/^[^:：]{1,64}[:：]\s*(\[语音\][\s\S]*)$/)
  if (withSender) body = withSender[1]
  return body.replace(/^\[语音\]\s*/, '').trim()
}

/**
 * 原文行里能匹配到语音时返回其 local_id，否则 null。
 *
 * 先按「去前缀后的全文」精确匹配；不相等（换行/截断）时退化为包含匹配，
 * 长文本优先，避免「好的」这类短句抢走别人的播放器。
 */
function matchVoiceLine(
  line: string,
  byText: Map<string, number>,
  keysByLength: string[],
): number | null {
  const text = voiceLineText(line)
  if (!text) return null
  const exact = byText.get(text)
  if (exact !== undefined) return exact
  for (const key of keysByLength) {
    if (key && key !== text && (text.includes(key) || key.includes(text))) {
      return byText.get(key) ?? null
    }
  }
  return null
}

/** 商机详情抽屉：原文/要点/上下文（独立加载）/回复框（@成员、引用）/操作。 */
export default function OpportunityDetail(props: {
  id: number | null
  onClose: () => void
}) {
  const { id, onClose } = props
  const queryClient = useQueryClient()
  const [replyText, setReplyText] = useState('')
  const [atMembers, setAtMembers] = useState<string[]>([])
  // 群内回复默认 @ 发送人（可取消，记住上次选择）
  const [atSender, setAtSender] = useState(readAtSenderPref)
  const [quoteLocalId, setQuoteLocalId] = useState<number | null>(null)
  // 「引用最新一条消息」是**意图**而不是某个固定 local_id：页面加载后
  // 群里随时会进来新消息（尤其是自己刚发的回复），若钉死 local_id，
  // 后端会把它当成「引用更早的一条」去文本定位，既慢又易失败。
  const [quoteLatest, setQuoteLatest] = useState(false)
  // 人工确认（用户反馈：存在发错人的情况——同名联系人/企业微信身份/操作失败）。
  // 默认开启：只把回复内容填进微信输入框，由用户在微信里核对收件人后
  // 自己点发送；关掉才是「自动发送 + 数据库确认」。
  const [manualConfirm, setManualConfirm] = useState(true)
  const [snack, setSnack] = useState<{ ok: boolean; message: string } | null>(null)
  // 发送中耗时（202 受理后要等微信 GUI 操作，必须让用户看到进度）
  const [sendingSince, setSendingSince] = useState<number | null>(null)
  // 这次发往哪里：群内（session）/ 私信（sender），只影响按钮文案
  const [sendingTarget, setSendingTarget] = useState<ReplyTargetKind>('session')
  const [sendingMs, setSendingMs] = useState(0)
  const timerRef = useRef<number | null>(null)
  // 本次发送是否已有 WS send_result 结算。0 秒失败（微信关到托盘）时 WS
  // 结果可能比 HTTP 202 响应更早到：随后到达的 onSuccess 若照常显示
  // 「已加入发送队列」，就会把失败提示覆盖掉，页面看起来像「只进队列、
  // 没有任何后续动作」。用同步 ref 判断，不受 React 批处理影响。
  const sendSettledRef = useRef(false)

  // 详情：只读业务库（不查微信库），秒开
  const { data: opp, isLoading } = useQuery({
    queryKey: ['opportunity', id],
    queryFn: () => apiGet<OpportunityDetail>(`/api/opportunities/${id}`),
    enabled: id !== null,
  })

  // 上下文：独立异步接口（微信库查询 + 后端瞬时故障重试；失败可重新加载）
  const contextQuery = useQuery({
    queryKey: ['opportunity-context', id],
    queryFn: () => apiGet<OpportunityContextOut>(`/api/opportunities/${id}/context`),
    enabled: id !== null,
  })
  const contextError = contextQuery.error
    ? (contextQuery.error as Error).message
    : contextQuery.data?.error ?? ''
  // 时间正序渲染（最新在最下）：后端返回时间倒序，按 sort_seq 升序排列
  const contextItems = useMemo(
    () =>
      [...(contextQuery.data?.items ?? [])].sort(
        (a, b) => a.sort_seq - b.sort_seq,
      ),
    [contextQuery.data],
  )

  // 原文（raw_text_display）里语音只是「[语音] 文本」纯文本，没有 local_id，
  // 用户看不到播放按钮（批次 6 需求 4）。用上下文里 type='语音' 的条目按
  // 「去掉 [语音] 前缀后的文本」建索引，匹配到的原文行就渲染成播放器。
  const voiceIndex = useMemo(() => {
    const byText = new Map<string, number>()
    for (const m of contextItems) {
      if (m.type !== '语音') continue
      const key = m.content.replace(/^\[语音\]\s*/, '').trim()
      // 同一句语音在窗口里出现多次时取第一条（不覆盖已有键）
      if (!key || byText.has(key)) continue
      byText.set(key, m.local_id)
    }
    // 兜底匹配用：长文本优先，避免短句抢走别人的播放器
    const keysByLength = [...byText.keys()].sort((a, b) => b.length - a.length)
    return { byText, keysByLength }
  }, [contextItems])

  const rawText = opp?.raw_text_display || opp?.raw_text || ''
  const rawLines = useMemo(() => rawText.split('\n'), [rawText])
  // 逐行尝试匹配语音；一行都匹配不到时保持原来的整段渲染（零行为变化）
  const rawVoiceIds = useMemo(
    () => rawLines.map(
      (line) => matchVoiceLine(line, voiceIndex.byText, voiceIndex.keysByLength),
    ),
    [rawLines, voiceIndex],
  )
  const hasRawVoice = rawVoiceIds.some((localId) => localId !== null)

  // 原文里的图片行（批次 11 需求 1）：后端把「该行完整原文 + local_id」落进
  // raw_refs，这里用**顺序消费指针**逐行对齐——refs 与原文行都按时间正序，
  // 同一句话重复出现也能各自对上（不靠文本哈希去重）。
  // 展示行(raw_text_display)与原始行(raw_text)一一对应（只换发送人前缀），
  // 两个都比一次，避免「群昵称替换后行文本不再与 refs 相等」而漏图。
  const rawSourceLines = useMemo(
    () => (opp?.raw_text || '').split('\n'),
    [opp?.raw_text],
  )
  const rawImageIds = useMemo(() => {
    const refs = opp?.raw_refs ?? []
    const ids: (number | null)[] = rawLines.map(() => null)
    let cursor = 0
    for (let idx = 0; idx < rawLines.length && cursor < refs.length; idx += 1) {
      const ref = refs[cursor]
      if (!ref || typeof ref.line !== 'string') break
      if (rawLines[idx] !== ref.line && rawSourceLines[idx] !== ref.line) continue
      const localId = Number(ref.local_id)
      if (Number.isFinite(localId) && localId > 0) ids[idx] = localId
      cursor += 1 // 匹配成功才前进：该行消费掉对应的 ref
    }
    return ids
  }, [opp?.raw_refs, rawLines, rawSourceLines])
  const hasRawImage = rawImageIds.some((localId) => localId !== null)
  // 有语音或有图片才需要逐行渲染；两者都没有时保持原来的整段渲染
  const needLineRender = hasRawVoice || hasRawImage

  // @ 群成员候选：群会话才有（后端按会话读微信库成员表，600s 缓存）
  const isGroup = (opp?.session_username ?? '').endsWith('@chatroom')
  const membersQuery = useQuery({
    queryKey: ['opportunity-members', id],
    queryFn: () => apiGet<GroupMembers>(`/api/opportunities/${id}/members`),
    enabled: id !== null && isGroup,
    staleTime: 5 * 60 * 1000,
  })
  const memberNames = useMemo(
    () => (membersQuery.data?.items ?? []).map((m) => m.name),
    [membersQuery.data],
  )

  // 回复去向（第 15 批）：群商机才有「群内 / 私信」两种，能否私信由后端
  // 判「发送人是不是好友」——群里陌生人（非好友）私信按钮必须置灰，
  // 否则点下去只会在微信里搜不到人。
  const replyTargetQuery = useQuery({
    queryKey: ['opportunity-reply-target', id],
    queryFn: () => apiGet<ReplyTarget>(`/api/opportunities/${id}/reply-targets`),
    enabled: id !== null && isGroup,
    staleTime: 60 * 1000,
  })
  const replyTarget = replyTargetQuery.data
  const canDm = Boolean(isGroup && replyTarget?.sender_is_friend)
  const dmDisabledReason = replyTarget?.reason
    || (replyTargetQuery.isLoading ? '正在确认对方是否为好友…' : '')
  // @ 用的发送人名：优先后端实时群昵称（改名后仍能 @ 到），
  // 其次详情里已解析的显示名
  const senderName = replyTarget?.sender_name
    || opp?.sender_display || opp?.sender_name || ''
  // 实际 @ 名单：勾选时把发送人放最前，再拼用户手选的成员（去重）
  const effectiveAt = useMemo(() => {
    const names: string[] = []
    if (isGroup && atSender && senderName) names.push(senderName)
    for (const raw of atMembers) {
      const name = raw.trim()
      if (name && !names.includes(name)) names.push(name)
    }
    return names
  }, [isGroup, atSender, senderName, atMembers])

  // 切换商机时清空草稿（@ 名单/引用是上一条商机的，带过去会发错人）
  useEffect(() => {
    setReplyText('')
    setAtMembers([])
    setQuoteLocalId(null)
    setQuoteLatest(false)
    sendSettledRef.current = false
  }, [id])

  // 发送结果经 WS → CustomEvent 送达（202 受理后异步反馈）
  useEffect(() => {
    const handler = (ev: Event) => {
      const data = (ev as CustomEvent<SendResultData>).detail
      if (!id || data.opportunity_id !== id) return
      sendSettledRef.current = true
      const detail = stageText(data)
      setSnack({
        ok: data.ok,
        message: detail ? `${data.message}（${detail}）` : data.message,
      })
      setSendingSince(null)
      if (data.ok) {
        // 已发出：清空草稿，避免用户重复点发送
        setReplyText('')
        setAtMembers([])
        setQuoteLocalId(null)
      }
      void queryClient.invalidateQueries({ queryKey: ['opportunity', id] })
      void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
    }
    window.addEventListener('kefu-send-result', handler)
    return () => window.removeEventListener('kefu-send-result', handler)
  }, [id, queryClient])

  // 发送中计时（微信 GUI 操作要数秒，没有任何反馈会让人以为卡死）
  useEffect(() => {
    if (sendingSince === null) {
      setSendingMs(0)
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current)
        timerRef.current = null
      }
      return
    }
    timerRef.current = window.setInterval(() => {
      setSendingMs(Date.now() - sendingSince)
    }, 200)
    return () => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current)
        timerRef.current = null
      }
    }
  }, [sendingSince])

  const statusMutation = useMutation({
    mutationFn: (status: string) =>
      apiPatch(`/api/opportunities/${id}`, { status }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
      void queryClient.invalidateQueries({ queryKey: ['opportunity', id] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
  })

  const replyMutation = useMutation({
    mutationFn: (payload: ReplyPayload) =>
      apiPost<{ ok: boolean; message: string }>(
        `/api/opportunities/${id}/reply`,
        payload,
      ),
    onSuccess: (resp) => {
      // 202 受理：最终结果等 WS send_result。若结果已经先一步送达并显示，
      // 绝不能用「已加入发送队列」把它覆盖（0 秒失败的时序竞态）。
      if (sendSettledRef.current) return
      setSnack({ ok: resp.ok, message: resp.message })
    },
    onError: (err: Error) => {
      setSendingSince(null)
      if (sendSettledRef.current) return
      setSnack({ ok: false, message: err.message })
    },
  })

  const handleSend = (target: ReplyTargetKind = 'session') => {
    if (!replyText.trim()) return
    if (target === 'sender' && !canDm) return
    const payload: ReplyPayload = {
      text: replyText.trim(),
      // 群内回复带 @（默认含发送人）；私信回复不带 @，也不带引用
      // ——引用的那条消息在群会话里，私信窗口里根本不存在
      at: target === 'sender' ? [] : effectiveAt,
      target,
      manual_confirm: manualConfirm,
    }
    if (target === 'session' && quoteLocalId !== null) {
      // latest=true：后端在真正执行发送时右键当时最新的一条，不受页面
      // 加载后新消息（含自己上一条回复）影响
      payload.quote = { local_id: quoteLocalId, latest: quoteLatest }
    }
    sendSettledRef.current = false
    setSendingTarget(target)
    setSendingSince(Date.now())
    replyMutation.mutate(payload)
  }

  // 跳转微信（反馈 ⑦）：调起微信并打开该会话的输入窗口（用户手动回复）
  const jumpMutation = useMutation({
    mutationFn: () =>
      apiPost<WechatJumpResult>('/api/wechat/jump', {
        session_username: opp?.session_username ?? '',
        session_name: opp?.session_display || opp?.session_name || '',
      }),
    onSuccess: (resp) => setSnack({ ok: resp.ok, message: resp.message }),
    onError: (err: Error) => setSnack({ ok: false, message: err.message }),
  })

  /** 引用某条上下文消息（再点一次取消）；latest=引用执行时的最新一条。 */
  const toggleQuote = (localId: number, latest = false) => {
    if (quoteLocalId === localId && quoteLatest === latest) {
      setQuoteLocalId(null)
      setQuoteLatest(false)
      return
    }
    setQuoteLocalId(localId)
    setQuoteLatest(latest)
  }

  const clearQuote = () => {
    setQuoteLocalId(null)
    setQuoteLatest(false)
  }

  const quotedItem = quoteLocalId === null
    ? null
    : contextItems.find((m) => m.local_id === quoteLocalId) ?? null

  return (
    <Drawer anchor="right" open={id !== null} onClose={onClose}>
      <Box sx={{ width: 560, p: 2 }}>
        <Stack direction="row" alignItems="center" sx={{ mb: 1 }}>
          <Typography variant="subtitle1" sx={{ flexGrow: 1 }}>
            {opp ? `${opp.type_name} · ${opp.session_display || opp.session_name}` : '商机详情'}
          </Typography>
          {opp && (
            <Tooltip title="跳转微信：打开该会话的输入窗口（手动回复）">
              <span>
                <IconButton
                  size="small"
                  disabled={jumpMutation.isPending}
                  onClick={() => jumpMutation.mutate()}
                >
                  <OpenInNewIcon fontSize="small" />
                </IconButton>
              </span>
            </Tooltip>
          )}
          <IconButton onClick={onClose} size="small">
            <CloseIcon />
          </IconButton>
        </Stack>

        {isLoading && <LinearProgress />}
        {opp && (
          <>
            <Box sx={{ bgcolor: '#f7f7f7', p: 1.5, borderRadius: 1, mb: 1.5 }}>
              <Stack direction="row" spacing={1} sx={{ mb: 1, flexWrap: 'wrap' }} useFlexGap>
                <Chip size="small" label={`发送人：${opp.sender_display || opp.sender_name}`} />
                {opp.sender_wxid && <Chip size="small" label={`wxid：${opp.sender_wxid}`} />}
                <Chip size="small" label={`窗口：${opp.window_start.replace('T', ' ')} ~ ${opp.window_end.replace('T', ' ')}`} />
                <Chip size="small" label={`合并 ${opp.merged_count} 条`} />
                <Chip size="small" color="primary" label={`置信度 ${opp.confidence}`} />
              </Stack>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>AI 需求要点</Typography>
              <Typography variant="body2" sx={{ whiteSpace: 'pre-line', mb: 1 }}>
                {opp.summary || '（无）'}
              </Typography>
              {opp.key_quote && (
                <>
                  <Typography variant="subtitle2" sx={{ mb: 0.5 }}>关键原文摘录</Typography>
                  <Typography variant="body2" color="secondary">「{opp.key_quote}」</Typography>
                </>
              )}
            </Box>

            <Typography variant="subtitle2">消息原文（合并窗口）</Typography>
            <Box sx={{ bgcolor: '#fbfbfb', border: '1px solid #eee', borderRadius: 1, p: 1.5, mb: 1.5 }}>
              {needLineRender ? (
                // 含语音/图片：逐行渲染，语音行给播放按钮、图片行给真图
                // （其余文本与原文完全一致，仅多出播放器/图片）
                <Box>
                  {rawLines.map((line, idx) => {
                    const imageId = rawImageIds[idx]
                    if (imageId != null && opp) {
                      // 图片行：渲染真图，并保留原文本小字说明（识别结论
                      // 就写在那行里，如「[图片] 识别：车辆/汽车配件（…）」）
                      return (
                        <Box key={`raw-image-${idx}-${imageId}`} sx={{ mb: 1 }}>
                          <ContextImage
                            username={opp.session_username}
                            localId={imageId}
                            fallback={line}
                          />
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{ display: 'block', whiteSpace: 'pre-wrap', mt: 0.5 }}
                          >
                            {line}
                          </Typography>
                        </Box>
                      )
                    }
                    const localId = rawVoiceIds[idx]
                    if (localId == null || !opp) {
                      return (
                        <Typography
                          key={`raw-${idx}`}
                          variant="body2"
                          sx={{ whiteSpace: 'pre-wrap', minHeight: line ? undefined : '1em' }}
                        >
                          {line}
                        </Typography>
                      )
                    }
                    return (
                      <VoicePlayer
                        key={`raw-voice-${idx}-${localId}`}
                        username={opp.session_username}
                        localId={localId}
                        text={voiceLineText(line)}
                      />
                    )
                  })}
                </Box>
              ) : (
                <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                  {/* 群消息每行显示群昵称而非 wxid（后端已解析，反馈 6） */}
                  {opp.raw_text_display || opp.raw_text}
                </Typography>
              )}
            </Box>

            <Typography variant="subtitle2">上下文消息（最近 11 条，时间正序）</Typography>
            <Box sx={{ maxHeight: 200, overflow: 'auto', border: '1px solid #eee', borderRadius: 1, mb: 1.5 }}>
              {/* 上下文独立加载态：Skeleton 骨架屏（详情本身秒开，不互相阻塞） */}
              {contextQuery.isLoading && (
                <Box sx={{ p: 1.5 }}>
                  {[0, 1, 2].map((i) => (
                    <Box key={i} sx={{ mb: 1.5 }}>
                      <Skeleton variant="text" width="45%" height={18} />
                      <Skeleton variant="text" width="85%" height={18} />
                    </Box>
                  ))}
                </Box>
              )}
              {/* 读取失败：明确中文原因 + 重新加载按钮 */}
              {contextError && (
                <Alert
                  severity="warning"
                  sx={{ m: 1.5 }}
                  action={
                    <Button
                      color="inherit"
                      size="small"
                      startIcon={<RefreshIcon />}
                      onClick={() => contextQuery.refetch()}
                    >
                      重新加载
                    </Button>
                  }
                >
                  {contextError}
                </Alert>
              )}
              {!contextQuery.isLoading && !contextError && contextItems.map((m) => (
                <Box
                  key={`${m.sort_seq}-${m.local_id}`}
                  sx={{
                    px: 1.5,
                    py: 0.5,
                    borderBottom: '1px dashed #eee',
                    bgcolor: quoteLocalId === m.local_id ? '#eef6ff' : 'transparent',
                  }}
                >
                  <Stack direction="row" alignItems="center" spacing={0.5}>
                    <Typography variant="caption" color="text.secondary" sx={{ flexGrow: 1 }}>
                      {m.create_time.replace('T', ' ')} · {m.sender_name} · {m.type_name}
                    </Typography>
                    <Tooltip title={quoteLocalId === m.local_id ? '取消引用' : '引用这条消息'}>
                      <IconButton
                        size="small"
                        color={quoteLocalId === m.local_id ? 'primary' : 'default'}
                        onClick={() => toggleQuote(m.local_id)}
                      >
                        <FormatQuoteIcon fontSize="inherit" />
                      </IconButton>
                    </Tooltip>
                  </Stack>
                  {m.type === '图片' && opp ? (
                    <ContextImage
                      username={opp.session_username}
                      localId={m.local_id}
                      fallback={m.content}
                    />
                  ) : m.type === '语音' && opp ? (
                    // 语音消息（反馈 ⑧）：播放按钮 + 转写文本（去掉「[语音]」前缀）
                    <VoicePlayer
                      username={opp.session_username}
                      localId={m.local_id}
                      text={m.content.replace(/^\[语音\]\s*/, '')}
                    />
                  ) : (
                    <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                      {m.content}
                    </Typography>
                  )}
                </Box>
              ))}
              {!contextQuery.isLoading && !contextError && contextItems.length === 0 && (
                <Typography variant="caption" color="text.secondary" sx={{ p: 1.5, display: 'block' }}>
                  暂无上下文消息
                  <Button size="small" startIcon={<RefreshIcon />} onClick={() => contextQuery.refetch()}>
                    重新加载
                  </Button>
                </Typography>
              )}
            </Box>

            {opp.reply_content && (
              <>
                <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
                  <Typography variant="subtitle2">
                    已回复内容（{opp.replied_at?.replace('T', ' ')}）
                  </Typography>
                  {/* 私信回的和群里回的不一样：私信只有发送人看到，
                      群里其他成员看不到，必须标出来 */}
                  {isGroup && (
                    <Chip
                      size="small"
                      variant="outlined"
                      color={opp.reply_target === 'sender' ? 'secondary' : 'default'}
                      label={opp.reply_target === 'sender' ? '私信回复' : '群内回复'}
                    />
                  )}
                </Stack>
                <Typography variant="body2" sx={{ mb: 1 }}>{opp.reply_content}</Typography>
              </>
            )}

            <Divider sx={{ mb: 1.5 }} />

            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              回复目标：{opp.session_display || opp.session_name}
              {isGroup && senderName && `（发送人：${senderName}）`}
              {opp.status === 'replied' && '（已回复过，可继续发送）'}
            </Typography>
            <TextField
              fullWidth
              multiline
              minRows={3}
              placeholder={manualConfirm
                ? '输入要填入微信输入框的内容（Ctrl+Enter 填入）'
                : '输入回复内容（Ctrl+Enter 发送）'}
              value={replyText}
              onChange={(e) => setReplyText(e.target.value)}
              onKeyDown={(e) => {
                if (e.ctrlKey && e.key === 'Enter') handleSend()
              }}
              sx={{ mb: 1 }}
            />
            {/* 人工确认开关（默认开启）：发送前让人看一眼收件人，专治同名
                联系人 / 企业微信身份 / 操作失败导致发错人的情况 */}
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={manualConfirm}
                  onChange={(e) => setManualConfirm(e.target.checked)}
                />
              }
              label={
                <Typography variant="body2">
                  手动确认（只填入微信输入框，由你在微信里核对后点发送）
                </Typography>
              }
              sx={{ mb: 1, alignItems: 'flex-start', '& .MuiFormControlLabel-label': { pt: 0.5 } }}
            />
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1, flexWrap: 'wrap' }} useFlexGap>
              {isGroup && (
                // 群内回复默认 @ 发送人：取消勾选后不再 @，选择记在本地
                // （下次打开还是上次的选择）
                <FormControlLabel
                  sx={{ m: 0 }}
                  control={
                    <Checkbox
                      size="small"
                      checked={atSender}
                      disabled={!senderName}
                      onChange={(e) => {
                        setAtSender(e.target.checked)
                        saveAtSenderPref(e.target.checked)
                      }}
                    />
                  }
                  label={
                    <Typography variant="body2">
                      @发送人{senderName ? `（${senderName}）` : ''}
                    </Typography>
                  }
                />
              )}
              {isGroup && (
                <Autocomplete
                  multiple
                  freeSolo
                  size="small"
                  options={memberNames}
                  value={atMembers}
                  onChange={(_e, value) => setAtMembers(value)}
                  loading={membersQuery.isLoading}
                  noOptionsText={membersQuery.data?.error
                    ? '读取群成员失败，可直接输入群昵称后回车'
                    : '没有匹配的群成员，可直接输入群昵称后回车'}
                  filterSelectedOptions
                  sx={{ width: 340 }}
                  renderInput={(params) => (
                    <TextField
                      {...params}
                      label="@其他群成员（可选）"
                      placeholder={atMembers.length ? '' : '选择还要 @ 的人'}
                      helperText={membersQuery.data?.error
                        ? membersQuery.data.error
                        : undefined}
                      error={Boolean(membersQuery.data?.error)}
                    />
                  )}
                />
              )}
              {quoteLocalId !== null ? (
                <Chip
                  color="primary"
                  variant="outlined"
                  size="small"
                  icon={<FormatQuoteIcon />}
                  label={quoteLatest
                    ? '引用最新一条消息'
                    : `引用：${(quotedItem?.content || quotedItem?.type_name || '所选消息').slice(0, 16)}`}
                  onDelete={clearQuote}
                />
              ) : (
                contextItems.length > 0 && (
                  <Button
                    size="small"
                    startIcon={<FormatQuoteIcon />}
                    onClick={() => toggleQuote(contextItems[contextItems.length - 1].local_id, true)}
                  >
                    引用最新一条消息
                  </Button>
                )
              )}
            </Stack>
            {/* 手动确认只「填文字」：微信的 @ 必须在输入时从弹层里选人，
                纯粘贴实现不了真 @。提前说清楚，并给一键改成自动发送。 */}
            {manualConfirm && isGroup && (effectiveAt.length > 0 || quoteLocalId !== null) && (
              <Alert
                severity="info"
                sx={{ mb: 1, py: 0 }}
                action={
                  <Button color="inherit" size="small" onClick={() => setManualConfirm(false)}>
                    改为自动发送
                  </Button>
                }
              >
                手动确认模式只把文字填进输入框，
                {effectiveAt.length > 0 ? '@' : ''}
                {effectiveAt.length > 0 && quoteLocalId !== null ? '和' : ''}
                {quoteLocalId !== null ? '引用' : ''}
                不会生效（可在微信里自己补）。要真正 @ 到人，请点「改为自动发送」。
              </Alert>
            )}
            {sendingSince !== null && (
              <Alert
                severity="info"
                sx={{ mb: 1, py: 0 }}
                action={
                  <Button
                    color="inherit"
                    size="small"
                    onClick={() => setSendingSince(null)}
                  >
                    不再等待
                  </Button>
                }
              >
                已受理，正在操作微信窗口（{(sendingMs / 1000).toFixed(1)}s）…
                {manualConfirm
                  ? (sendingTarget === 'sender'
                    ? '会把内容填进与发送人的私信输入框，填完请在微信里核对后点发送；'
                    : '会把内容填进微信输入框，填完请在微信里核对后点发送；')
                  : ''}
                首次发送需初始化自动化组件，通常 3~8 秒；若超过 30 秒请检查微信窗口是否被遮挡。
              </Alert>
            )}
            <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap' }} useFlexGap>
              <Tooltip
                title={isGroup
                  ? `发到群里（${opp.session_display || opp.session_name}）${manualConfirm ? '，只填进输入框' : ''}`
                  : ''}
              >
                <span>
                  <Button
                    variant="contained"
                    startIcon={<SendIcon />}
                    disabled={replyMutation.isPending || sendingSince !== null || !replyText.trim()}
                    onClick={() => handleSend('session')}
                  >
                    {sendingSince !== null && sendingTarget === 'session'
                      ? (manualConfirm ? '填入中…' : '发送中…')
                      : isGroup
                        ? (manualConfirm ? '群内回复（填入输入框）' : '群内回复')
                        : (manualConfirm ? '填入微信输入框' : '发送回复')}
                  </Button>
                </span>
              </Tooltip>
              {/* 私信回复（第 15 批）：群商机专用，发给该条消息的发送人。
                  对方不是好友时微信里根本开不出会话，按钮置灰并说明原因。 */}
              {isGroup && (
                <Tooltip
                  title={canDm
                    ? `私信发给 ${senderName || '发送人'}（群里其他人看不到）`
                    : (dmDisabledReason || '对方不可私信')}
                >
                  <span>
                    <Button
                      startIcon={<ForwardToInboxIcon />}
                      disabled={
                        !canDm || replyMutation.isPending
                        || sendingSince !== null || !replyText.trim()
                      }
                      onClick={() => handleSend('sender')}
                    >
                      {sendingSince !== null && sendingTarget === 'sender'
                        ? (manualConfirm ? '填入中…' : '发送中…')
                        : '私信回复'}
                    </Button>
                  </span>
                </Tooltip>
              )}
              {/* 只跳转、不发消息：用户想直接在微信里自己处理（弹窗点进来时最常用）。
                  与右上角的跳转图标功能相同，这里放在操作行更好找（原图标保留）。 */}
              <Tooltip title="只跳转到消息来源会话（不发送任何消息），在微信里自己处理">
                <span>
                  <Button
                    startIcon={<OpenInNewIcon />}
                    disabled={jumpMutation.isPending}
                    onClick={() => jumpMutation.mutate()}
                  >
                    跳转微信会话
                  </Button>
                </span>
              </Tooltip>
              {opp.status !== 'ignored' ? (
                <Button
                  startIcon={<VisibilityOffIcon />}
                  onClick={() => statusMutation.mutate('ignored')}
                >
                  标记非商机
                </Button>
              ) : (
                <Tooltip title="恢复为待处理">
                  <Button startIcon={<UndoIcon />} onClick={() => statusMutation.mutate('pending')}>
                    恢复待处理
                  </Button>
                </Tooltip>
              )}
              {opp.status !== 'handled' && (
                <Tooltip title="已在微信里自行处理完，无需系统跟进">
                  <Button
                    startIcon={<TaskAltIcon />}
                    onClick={() => statusMutation.mutate('handled')}
                  >
                    已自行处理
                  </Button>
                </Tooltip>
              )}
              {opp.status === 'handled' && (
                <Tooltip title="恢复为待处理">
                  <Button startIcon={<UndoIcon />} onClick={() => statusMutation.mutate('pending')}>
                    恢复待处理
                  </Button>
                </Tooltip>
              )}
              {opp.status === 'replied' && (
                <Button onClick={() => statusMutation.mutate('pending')}>标记待处理</Button>
              )}
            </Stack>
          </>
        )}

        <Snackbar
          open={snack !== null}
          autoHideDuration={6000}
          onClose={() => setSnack(null)}
          message={snack?.message}
          anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
          ContentProps={{ sx: { bgcolor: snack?.ok ? 'success.main' : 'error.main' } }}
        />
      </Box>
    </Drawer>
  )
}

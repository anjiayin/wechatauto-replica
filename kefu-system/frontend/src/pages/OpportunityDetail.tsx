import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
  Divider,
  Drawer,
  IconButton,
  LinearProgress,
  Skeleton,
  Snackbar,
  Stack,
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
import { apiGet, apiPatch, apiPost } from '../api/client'
import type {
  GroupMembers,
  OpportunityContextOut,
  OpportunityDetail,
  ReplyPayload,
  WechatJumpResult,
} from '../api/types'

interface SendResultData {
  opportunity_id: number
  ok: boolean
  message: string
  /** 发送链路分段耗时（毫秒）：init/front/open/send */
  stages?: Record<string, number>
  total_ms?: number
}

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

/** 上下文中的图片消息：尝试展示解密真图，加载失败回退占位文本（反馈 2）。 */
function ContextImage(props: { username: string; localId: number; fallback: string }) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
        {props.fallback || '[图片]'}
      </Typography>
    )
  }
  return (
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
  )
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
  const [quoteLocalId, setQuoteLocalId] = useState<number | null>(null)
  const [snack, setSnack] = useState<{ ok: boolean; message: string } | null>(null)
  // 发送中耗时（202 受理后要等微信 GUI 操作，必须让用户看到进度）
  const [sendingSince, setSendingSince] = useState<number | null>(null)
  const [sendingMs, setSendingMs] = useState(0)
  const timerRef = useRef<number | null>(null)

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

  // 发送结果经 WS → CustomEvent 送达（202 受理后异步反馈）
  useEffect(() => {
    const handler = (ev: Event) => {
      const data = (ev as CustomEvent<SendResultData>).detail
      if (!id || data.opportunity_id !== id) return
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
      // 202 受理：最终结果等 WS send_result；这里先提示受理状态
      setSnack({ ok: resp.ok, message: resp.message })
    },
    onError: (err: Error) => {
      setSendingSince(null)
      setSnack({ ok: false, message: err.message })
    },
  })

  const handleSend = () => {
    if (!replyText.trim()) return
    const payload: ReplyPayload = {
      text: replyText.trim(),
      at: atMembers,
    }
    if (quoteLocalId !== null) {
      payload.quote = { local_id: quoteLocalId }
    }
    setSendingSince(Date.now())
    replyMutation.mutate(payload)
  }

  // 跳转微信（反馈 ⑦）：调起微信并打开该会话的输入窗口（用户手动回复）
  const jumpMutation = useMutation({
    mutationFn: () =>
      apiPost<WechatJumpResult>('/api/wechat/jump', {
        session_username: opp?.session_username ?? '',
        session_name: opp?.session_name ?? '',
      }),
    onSuccess: (resp) => setSnack({ ok: resp.ok, message: resp.message }),
    onError: (err: Error) => setSnack({ ok: false, message: err.message }),
  })

  /** 引用某条上下文消息（再点一次取消）。 */
  const toggleQuote = (localId: number) => {
    setQuoteLocalId((cur) => (cur === localId ? null : localId))
  }

  const quotedItem = quoteLocalId === null
    ? null
    : contextItems.find((m) => m.local_id === quoteLocalId) ?? null

  return (
    <Drawer anchor="right" open={id !== null} onClose={onClose}>
      <Box sx={{ width: 560, p: 2 }}>
        <Stack direction="row" alignItems="center" sx={{ mb: 1 }}>
          <Typography variant="subtitle1" sx={{ flexGrow: 1 }}>
            {opp ? `${opp.type_name} · ${opp.session_name}` : '商机详情'}
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
                <Chip size="small" label={`发送人：${opp.sender_name}`} />
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
              <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                {/* 群消息每行显示群昵称而非 wxid（后端已解析，反馈 6） */}
                {opp.raw_text_display || opp.raw_text}
              </Typography>
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
                <Typography variant="subtitle2">已回复内容（{opp.replied_at?.replace('T', ' ')}）</Typography>
                <Typography variant="body2" sx={{ mb: 1 }}>{opp.reply_content}</Typography>
              </>
            )}

            <Divider sx={{ mb: 1.5 }} />

            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              回复目标：{opp.session_name}
              {opp.status === 'replied' && '（已回复过，可继续发送）'}
            </Typography>
            <TextField
              fullWidth
              multiline
              minRows={3}
              placeholder="输入回复内容（Ctrl+Enter 发送）"
              value={replyText}
              onChange={(e) => setReplyText(e.target.value)}
              onKeyDown={(e) => {
                if (e.ctrlKey && e.key === 'Enter') handleSend()
              }}
              sx={{ mb: 1 }}
            />
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1, flexWrap: 'wrap' }} useFlexGap>
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
                      label="@群成员（从列表选择）"
                      placeholder={atMembers.length ? '' : '选择要 @ 的人'}
                      helperText={membersQuery.data?.error
                        ? membersQuery.data.error
                        : undefined}
                      error={Boolean(membersQuery.data?.error)}
                    />
                  )}
                />
              )}
              {quotedItem ? (
                <Chip
                  color="primary"
                  variant="outlined"
                  size="small"
                  icon={<FormatQuoteIcon />}
                  label={`引用：${(quotedItem.content || quotedItem.type_name).slice(0, 16)}`}
                  onDelete={() => setQuoteLocalId(null)}
                />
              ) : (
                contextItems.length > 0 && (
                  <Button
                    size="small"
                    startIcon={<FormatQuoteIcon />}
                    onClick={() => toggleQuote(contextItems[contextItems.length - 1].local_id)}
                  >
                    引用最新一条消息
                  </Button>
                )
              )}
            </Stack>
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
                首次发送需初始化自动化组件，通常 3~8 秒；若超过 30 秒请检查微信窗口是否被遮挡。
              </Alert>
            )}
            <Stack direction="row" spacing={1}>
              <Button
                variant="contained"
                startIcon={<SendIcon />}
                disabled={replyMutation.isPending || sendingSince !== null || !replyText.trim()}
                onClick={handleSend}
              >
                {sendingSince !== null ? '发送中…' : '发送回复'}
              </Button>
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

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
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
import { apiGet, apiPatch, apiPost } from '../api/client'
import type {
  OpportunityContextOut,
  OpportunityDetail,
  ReplyPayload,
} from '../api/types'

interface SendResultData {
  opportunity_id: number
  ok: boolean
  message: string
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

/** 商机详情抽屉：原文/要点/上下文（独立加载）/回复框（@成员、引用）/操作。 */
export default function OpportunityDetail(props: {
  id: number | null
  onClose: () => void
}) {
  const { id, onClose } = props
  const queryClient = useQueryClient()
  const [replyText, setReplyText] = useState('')
  const [atText, setAtText] = useState('')
  const [quoteLast, setQuoteLast] = useState(false)
  const [snack, setSnack] = useState<{ ok: boolean; message: string } | null>(null)

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

  // 发送结果经 WS → CustomEvent 送达（202 受理后异步反馈）
  useEffect(() => {
    const handler = (ev: Event) => {
      const data = (ev as CustomEvent<SendResultData>).detail
      if (!id || data.opportunity_id !== id) return
      setSnack({ ok: data.ok, message: data.message })
      void queryClient.invalidateQueries({ queryKey: ['opportunity', id] })
      void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
    }
    window.addEventListener('kefu-send-result', handler)
    return () => window.removeEventListener('kefu-send-result', handler)
  }, [id, queryClient])

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
      setSnack({ ok: false, message: err.message })
    },
  })

  const handleSend = () => {
    if (!replyText.trim()) return
    const payload: ReplyPayload = {
      text: replyText.trim(),
      at: atText
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean),
    }
    // 引用回复：引用上下文里最新一条消息（上下文 items 已按时间正序，取末条）
    if (quoteLast && contextItems.length > 0) {
      payload.quote = { local_id: contextItems[contextItems.length - 1].local_id }
    }
    replyMutation.mutate(payload)
  }

  return (
    <Drawer anchor="right" open={id !== null} onClose={onClose}>
      <Box sx={{ width: 560, p: 2 }}>
        <Stack direction="row" alignItems="center" sx={{ mb: 1 }}>
          <Typography variant="subtitle1" sx={{ flexGrow: 1 }}>
            {opp ? `${opp.type_name} · ${opp.session_name}` : '商机详情'}
          </Typography>
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
                {opp.raw_text}
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
                <Box key={`${m.sort_seq}-${m.local_id}`} sx={{ px: 1.5, py: 0.5, borderBottom: '1px dashed #eee' }}>
                  <Typography variant="caption" color="text.secondary">
                    {m.create_time.replace('T', ' ')} · {m.sender_name} · {m.type_name}
                  </Typography>
                  {m.type === '图片' && opp ? (
                    <ContextImage
                      username={opp.session_username}
                      localId={m.local_id}
                      fallback={m.content}
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
            <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1.5, flexWrap: 'wrap' }} useFlexGap>
              <TextField
                label="@群成员（多个用逗号分隔，仅群聊有效）"
                sx={{ width: 320 }}
                value={atText}
                onChange={(e) => setAtText(e.target.value)}
              />
              <FormControlLabel
                control={
                  <Checkbox
                    checked={quoteLast}
                    onChange={(e) => setQuoteLast(e.target.checked)}
                  />
                }
                label="引用最新一条消息"
              />
            </Stack>
            <Stack direction="row" spacing={1}>
              <Button
                variant="contained"
                startIcon={<SendIcon />}
                disabled={replyMutation.isPending || !replyText.trim()}
                onClick={handleSend}
              >
                {replyMutation.isPending ? '发送中…' : '发送回复'}
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

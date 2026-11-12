import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Box,
  Button,
  Chip,
  IconButton,
  MenuItem,
  Paper,
  Snackbar,
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
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff'
import ReplyIcon from '@mui/icons-material/Reply'
import TaskAltIcon from '@mui/icons-material/TaskAlt'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import CheckIcon from '@mui/icons-material/Check'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import { apiGet, apiPatch, apiPost, qs } from '../api/client'
import { consumePendingOpportunity } from '../hooks/pendingOpportunity'
import type {
  OpportunityListOut,
  OpportunityType,
  StatusOut,
  WechatJumpResult,
} from '../api/types'
import OpportunityDetail from './OpportunityDetail'

const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  replied: '已回复',
  ignored: '已忽略',
  handled: '已自行处理',
}

/** 买/卖货方向标签（反馈 4）：买=蓝、卖=橙、不明不显示 */
const DIRECTION_LABELS: Record<string, string> = {
  buy: '买货',
  sell: '卖货',
}

const STATUS_COLORS: Record<
  string,
  'warning' | 'success' | 'default' | 'info'
> = {
  pending: 'warning',
  replied: 'success',
  ignored: 'default',
  handled: 'info',
}

/** 行内一键复制图标：点击复制文本并短暂显示 ✓ 表示成功。 */
function CopyButton(props: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Tooltip title={copied ? '已复制' : `复制${props.label}`} arrow>
      <IconButton
        size="small"
        aria-label={`复制${props.label}`}
        sx={{ ml: 0.25, py: 0.25 }}
        onClick={(e) => {
          e.stopPropagation() // 不触发行点击（打开详情）
          navigator.clipboard.writeText(props.text).then(() => {
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          })
        }}
      >
        {copied ? (
          <CheckIcon sx={{ fontSize: 14, color: 'success.main' }} />
        ) : (
          <ContentCopyIcon sx={{ fontSize: 14 }} />
        )}
      </IconButton>
    </Tooltip>
  )
}

/** 超长原文 tooltip：支持换行与滚动，完整展示不截断。 */
const QUOTE_TOOLTIP_PROPS = {
  slotProps: {
    popper: {
      sx: {
        '& .MuiTooltip-tooltip': {
          maxWidth: 480,
          maxHeight: 320,
          overflowY: 'auto',
          whiteSpace: 'pre-wrap',
          fontSize: 13,
        },
      },
    },
  },
} as const

/** 商机列表页：筛选栏、未读角标联动、行内快捷操作、详情抽屉。 */
export default function OpportunityList() {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState('')
  const [typeId, setTypeId] = useState('')
  const [direction, setDirection] = useState('')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(20)
  const [detailId, setDetailId] = useState<number | null>(null)

  // 全局「新商机 toast 点击跳转」：App 派发 kefu-open-opportunity → 打开详情抽屉。
  // 列表页未挂载时事件丢失的竞态由 pending 机制兜底：挂载时消费待打开 id。
  useEffect(() => {
    const handler = (ev: Event) => {
      const d = (ev as CustomEvent<{ id: number }>).detail
      if (typeof d?.id === 'number') {
        consumePendingOpportunity() // 已挂载：事件直达，同步清空 pending 防陈旧
        setDetailId(d.id)
      }
    }
    const pending = consumePendingOpportunity()
    if (pending !== null) setDetailId(pending)
    window.addEventListener('kefu-open-opportunity', handler)
    return () => window.removeEventListener('kefu-open-opportunity', handler)
  }, [])

  const query = useMemo(
    () =>
      `/api/opportunities${qs({
        page: page + 1,
        page_size: pageSize,
        status,
        type_id: typeId,
        direction,
        keyword,
      })}`,
    [page, pageSize, status, typeId, direction, keyword],
  )

  const { data, isLoading } = useQuery({
    queryKey: ['opportunities', query],
    queryFn: () => apiGet<OpportunityListOut>(query),
  })

  const { data: types } = useQuery({
    queryKey: ['opportunity-types'],
    queryFn: () => apiGet<OpportunityType[]>('/api/opportunity-types'),
  })

  const { data: statusInfo } = useQuery({
    queryKey: ['status'],
    queryFn: () => apiGet<StatusOut>('/api/status'),
    refetchInterval: 5000,
  })

  const ignoreMutation = useMutation({
    mutationFn: (id: number) => apiPatch(`/api/opportunities/${id}`, { status: 'ignored' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
  })

  // 标记「已自行处理」：用户已在微信里自己处理完的商机
  const handleMutation = useMutation({
    mutationFn: (id: number) => apiPatch(`/api/opportunities/${id}`, { status: 'handled' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
  })

  // 全部已读（反馈 12）：一键清零未读角标
  const readAllMutation = useMutation({
    mutationFn: () => apiPost<{ cleared: number }>('/api/opportunities/read-all'),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['status'] })
    },
  })

  // 跳转微信（反馈 ⑦）：调起微信并打开该会话的输入窗口（用户手动回复）
  const [jumpSnack, setJumpSnack] = useState<{ ok: boolean; message: string } | null>(null)
  const jumpMutation = useMutation({
    mutationFn: (payload: { session_username: string; session_name: string }) =>
      apiPost<WechatJumpResult>('/api/wechat/jump', payload),
    onSuccess: (resp) => setJumpSnack({ ok: resp.ok, message: resp.message }),
    onError: (err: Error) => setJumpSnack({ ok: false, message: err.message }),
  })

  const items = data?.items ?? []

  /** 本地立即标记已读（反馈 3/9）：未读标识与角标秒级消失，不等轮询 */
  const markReadLocally = (id: number) => {
    let wasUnread = false
    for (const [, cached] of queryClient.getQueriesData<OpportunityListOut>({
      queryKey: ['opportunities'],
    })) {
      if (cached?.items.some((it) => it.id === id && it.unread)) {
        wasUnread = true
        break
      }
    }
    queryClient.setQueriesData<OpportunityListOut>({ queryKey: ['opportunities'] },
      (old) =>
        old
          ? { ...old, items: old.items.map((it) => (it.id === id ? { ...it, unread: false } : it)) }
          : old,
    )
    if (wasUnread) {
      queryClient.setQueryData<StatusOut>(['status'], (old) =>
        old ? { ...old, unread_count: Math.max(0, old.unread_count - 1) } : old,
      )
    }
  }

  /** 打开详情：先本地置为已读，再异步通知后端（幂等） */
  const openDetail = (id: number) => {
    setDetailId(id)
    markReadLocally(id)
    void apiPost(`/api/opportunities/${id}/read`).catch(() => undefined)
  }

  return (
    <Box>
      <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: 'wrap' }} useFlexGap>
        <TextField
          select
          label="状态"
          sx={{ width: 120 }}
          value={status}
          onChange={(e) => {
            setPage(0)
            setStatus(e.target.value)
          }}
        >
          <MenuItem value="">全部（不含忽略）</MenuItem>
          <MenuItem value="pending">待处理</MenuItem>
          <MenuItem value="replied">已回复</MenuItem>
          <MenuItem value="ignored">已忽略</MenuItem>
          <MenuItem value="handled">已自行处理</MenuItem>
        </TextField>
        <TextField
          select
          label="商机类型"
          sx={{ width: 180 }}
          value={typeId}
          onChange={(e) => {
            setPage(0)
            setTypeId(e.target.value)
          }}
        >
          <MenuItem value="">全部类型</MenuItem>
          {(types ?? []).map((t) => (
            <MenuItem key={t.id} value={String(t.id)}>
              {t.name}
            </MenuItem>
          ))}
        </TextField>
        <TextField
          select
          label="方向"
          sx={{ width: 110 }}
          value={direction}
          onChange={(e) => {
            setPage(0)
            setDirection(e.target.value)
          }}
        >
          <MenuItem value="">全部方向</MenuItem>
          <MenuItem value="buy">买货（求购）</MenuItem>
          <MenuItem value="sell">卖货（供货）</MenuItem>
        </TextField>
        <TextField
          label="关键词（要点/原文/发送人）"
          sx={{ width: 260 }}
          value={keyword}
          onChange={(e) => {
            setPage(0)
            setKeyword(e.target.value)
          }}
        />
        {statusInfo && statusInfo.unread_count > 0 && (
          <>
            <Chip color="error" size="small" label={`${statusInfo.unread_count} 条未读商机`} />
            <Button
              size="small"
              variant="outlined"
              disabled={readAllMutation.isPending}
              onClick={() => readAllMutation.mutate()}
            >
              全部已读
            </Button>
          </>
        )}
      </Stack>

      <Paper variant="outlined">
        <Box sx={{ overflowX: 'auto' }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell width={52}>未读</TableCell>
              <TableCell>类型</TableCell>
              <TableCell>来源</TableCell>
              <TableCell>发送人</TableCell>
              <TableCell>时间</TableCell>
              <TableCell>消息原文</TableCell>
              <TableCell>AI 要点摘要</TableCell>
              <TableCell align="right">置信度</TableCell>
              <TableCell>状态</TableCell>
              <TableCell align="right">操作</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={10}>加载中…</TableCell>
              </TableRow>
            )}
            {!isLoading && items.length === 0 && (
              <TableRow>
                <TableCell colSpan={10}>
                  <Typography color="text.secondary" align="center" sx={{ py: 4 }}>
                    暂无商机。请确认：① 设置页已配置监控对象与商机类型；② AI 接口可用（或在设置里改用轻量筛选）；③ 新消息在发送人停止发言（默认 15 秒）后即触发分析并产生商机。
                  </Typography>
                </TableCell>
              </TableRow>
            )}
            {items.map((o) => (
              <TableRow
                key={o.id}
                hover
                sx={{ cursor: 'pointer' }}
                onClick={() => openDetail(o.id)}
              >
                {/* 行内未读标识（反馈 9）：点击即消失并同步未读角标 */}
                <TableCell>
                  {o.unread ? (
                    <Tooltip title="未读，点击标记为已读" arrow>
                      <Chip
                        size="small"
                        color="error"
                        label="未读"
                        onClick={(e) => {
                          e.stopPropagation()
                          markReadLocally(o.id)
                          void apiPost(`/api/opportunities/${o.id}/read`).catch(() => undefined)
                        }}
                        sx={{ height: 20, fontSize: 11, cursor: 'pointer' }}
                      />
                    </Tooltip>
                  ) : (
                    <Typography variant="caption" color="text.disabled">—</Typography>
                  )}
                </TableCell>
                <TableCell>
                  <Stack direction="row" spacing={0.5} alignItems="center">
                    <Chip size="small" color="primary" variant="outlined" label={o.type_name} />
                    {o.direction && (
                      <Chip
                        size="small"
                        color={o.direction === 'buy' ? 'info' : 'warning'}
                        label={DIRECTION_LABELS[o.direction] ?? o.direction}
                      />
                    )}
                  </Stack>
                </TableCell>
                <TableCell>
                  <Box sx={{ display: 'flex', alignItems: 'center' }}>
                    <span>{o.session_name}</span>
                    <CopyButton text={o.session_name} label="群名/会话名" />
                  </Box>
                </TableCell>
                <TableCell>
                  <Box sx={{ display: 'flex', alignItems: 'center' }}>
                    <span>{o.sender_name}</span>
                    <CopyButton text={o.sender_name} label="发送人" />
                  </Box>
                  {o.sender_wxid && (
                    <Typography variant="caption" display="block" color="text.secondary">
                      {o.sender_wxid}
                    </Typography>
                  )}
                </TableCell>
                <TableCell sx={{ whiteSpace: 'nowrap' }}>{o.window_end.replace('T', ' ')}</TableCell>
                <TableCell sx={{ maxWidth: 260, minWidth: 160 }}>
                  {o.key_quote ? (
                    <Box sx={{ display: 'flex', alignItems: 'center' }}>
                      <Tooltip
                        title={o.key_quote}
                        placement="top"
                        arrow
                        {...QUOTE_TOOLTIP_PROPS}
                      >
                        <Typography
                          variant="body2"
                          sx={{
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {o.key_quote}
                        </Typography>
                      </Tooltip>
                      {/* 一键复制消息原文（反馈 10） */}
                      <CopyButton text={o.key_quote} label="消息原文" />
                    </Box>
                  ) : (
                    <Typography variant="body2" color="text.secondary">—</Typography>
                  )}
                </TableCell>
                <TableCell sx={{ maxWidth: 360 }}>
                  <Box sx={{ display: 'flex', alignItems: 'flex-start' }}>
                    <Typography variant="body2" sx={{ whiteSpace: 'pre-line', flexGrow: 1 }}>
                      {o.summary.length > 120 ? `${o.summary.slice(0, 120)}…` : o.summary}
                    </Typography>
                    {/* 一键复制 AI 要点摘要（反馈 10） */}
                    {o.summary && <CopyButton text={o.summary} label="要点摘要" />}
                  </Box>
                </TableCell>
                <TableCell align="right">{o.confidence}</TableCell>
                <TableCell>
                  <Chip size="small" color={STATUS_COLORS[o.status]} label={STATUS_LABELS[o.status]} />
                </TableCell>
                <TableCell align="right" onClick={(e) => e.stopPropagation()}>
                  <Tooltip title="去回复">
                    <IconButton size="small" onClick={() => openDetail(o.id)}>
                      <ReplyIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title="跳转微信：打开该会话的输入窗口（手动回复）">
                    <span>
                      <IconButton
                        size="small"
                        disabled={jumpMutation.isPending}
                        onClick={() =>
                          jumpMutation.mutate({
                            session_username: o.session_username,
                            session_name: o.session_name,
                          })
                        }
                      >
                        <OpenInNewIcon fontSize="small" />
                      </IconButton>
                    </span>
                  </Tooltip>
                  <Tooltip title="标记非商机">
                    <IconButton
                      size="small"
                      disabled={o.status === 'ignored'}
                      onClick={() => ignoreMutation.mutate(o.id)}
                    >
                      <VisibilityOffIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title="标记已自行处理（已在微信里处理完）">
                    <IconButton
                      size="small"
                      disabled={o.status === 'handled'}
                      onClick={() => handleMutation.mutate(o.id)}
                    >
                      <TaskAltIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </TableCell>
              </TableRow>
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

      <OpportunityDetail id={detailId} onClose={() => setDetailId(null)} />

      {/* 跳转微信结果 toast（UIA 操作 0.5~5s，成功/失败都要给反馈） */}
      <Snackbar
        open={jumpSnack !== null}
        autoHideDuration={6000}
        onClose={() => setJumpSnack(null)}
        message={jumpSnack?.message}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
        ContentProps={{ sx: { bgcolor: jumpSnack?.ok ? 'success.main' : 'error.main' } }}
      />
    </Box>
  )
}

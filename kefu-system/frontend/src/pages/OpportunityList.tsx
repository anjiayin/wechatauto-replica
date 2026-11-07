import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Box,
  Button,
  Chip,
  IconButton,
  MenuItem,
  Paper,
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
import { apiGet, apiPatch, qs } from '../api/client'
import type {
  OpportunityListOut,
  OpportunityType,
  StatusOut,
} from '../api/types'
import OpportunityDetail from './OpportunityDetail'

const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  replied: '已回复',
  ignored: '已忽略',
}

const STATUS_COLORS: Record<string, 'warning' | 'success' | 'default'> = {
  pending: 'warning',
  replied: 'success',
  ignored: 'default',
}

/** 商机列表页：筛选栏、未读角标联动、行内快捷操作、详情抽屉。 */
export default function OpportunityList() {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState('')
  const [typeId, setTypeId] = useState('')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(20)
  const [detailId, setDetailId] = useState<number | null>(null)

  const query = useMemo(
    () =>
      `/api/opportunities${qs({
        page: page + 1,
        page_size: pageSize,
        status,
        type_id: typeId,
        keyword,
      })}`,
    [page, pageSize, status, typeId, keyword],
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

  const items = data?.items ?? []

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
          label="关键词（要点/原文/发送人）"
          sx={{ width: 260 }}
          value={keyword}
          onChange={(e) => {
            setPage(0)
            setKeyword(e.target.value)
          }}
        />
        {statusInfo && statusInfo.unread_count > 0 && (
          <Chip color="error" size="small" label={`${statusInfo.unread_count} 条未读商机`} />
        )}
      </Stack>

      <Paper variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>类型</TableCell>
              <TableCell>来源</TableCell>
              <TableCell>发送人</TableCell>
              <TableCell>时间</TableCell>
              <TableCell>AI 要点摘要</TableCell>
              <TableCell align="right">置信度</TableCell>
              <TableCell>状态</TableCell>
              <TableCell align="right">操作</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={8}>加载中…</TableCell>
              </TableRow>
            )}
            {!isLoading && items.length === 0 && (
              <TableRow>
                <TableCell colSpan={8}>
                  <Typography color="text.secondary" align="center" sx={{ py: 4 }}>
                    暂无商机。请确认：① 设置页已配置监控对象与商机类型；② AI 接口可用；③ 新消息在发送人停止发言（默认 60 秒）后即触发分析并产生商机。
                  </Typography>
                </TableCell>
              </TableRow>
            )}
            {items.map((o) => (
              <TableRow
                key={o.id}
                hover
                sx={{ cursor: 'pointer' }}
                onClick={() => setDetailId(o.id)}
              >
                <TableCell>
                  <Chip size="small" color="primary" variant="outlined" label={o.type_name} />
                </TableCell>
                <TableCell>{o.session_name}</TableCell>
                <TableCell>
                  {o.sender_name}
                  {o.sender_wxid && (
                    <Typography variant="caption" display="block" color="text.secondary">
                      {o.sender_wxid}
                    </Typography>
                  )}
                </TableCell>
                <TableCell sx={{ whiteSpace: 'nowrap' }}>{o.window_end.replace('T', ' ')}</TableCell>
                <TableCell sx={{ maxWidth: 360 }}>
                  <Typography variant="body2" sx={{ whiteSpace: 'pre-line' }}>
                    {o.summary.length > 120 ? `${o.summary.slice(0, 120)}…` : o.summary}
                  </Typography>
                </TableCell>
                <TableCell align="right">{o.confidence}</TableCell>
                <TableCell>
                  <Chip size="small" color={STATUS_COLORS[o.status]} label={STATUS_LABELS[o.status]} />
                </TableCell>
                <TableCell align="right" onClick={(e) => e.stopPropagation()}>
                  <Tooltip title="去回复">
                    <IconButton size="small" onClick={() => setDetailId(o.id)}>
                      <ReplyIcon fontSize="small" />
                    </IconButton>
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
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
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
    </Box>
  )
}

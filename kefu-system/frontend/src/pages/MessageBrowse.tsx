import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Box,
  IconButton,
  InputAdornment,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  Paper,
  Skeleton,
  TextField,
  Typography,
} from '@mui/material'
import SearchIcon from '@mui/icons-material/Search'
import { apiGet, qs } from '../api/client'
import type { MessageItem, SessionItem } from '../api/types'

/** 消息浏览页（P0 只读简化版）：左侧会话搜索列表，右侧消息流（时间正序）。 */
export default function MessageBrowse() {
  const [keyword, setKeyword] = useState('')
  const [selected, setSelected] = useState<SessionItem | null>(null)

  const { data: sessions, isFetching } = useQuery({
    queryKey: ['sessions', keyword],
    queryFn: () =>
      apiGet<SessionItem[]>(`/api/sessions/search${qs({ keyword, limit: 50 })}`),
  })

  const { data: messages } = useQuery({
    queryKey: ['messages', selected?.username],
    queryFn: () =>
      apiGet<MessageItem[]>(
        `/api/messages/${encodeURIComponent(selected?.username ?? '')}${qs({ limit: 100 })}`,
      ),
    enabled: selected !== null,
  })

  return (
    <Box sx={{ display: 'flex', gap: 2, height: 'calc(100vh - 120px)' }}>
      {/* 左侧：会话列表 */}
      <Paper variant="outlined" sx={{ width: 300, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
        <Box sx={{ p: 1 }}>
          <TextField
            fullWidth
            label="搜索会话（群/好友）"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void refetchHint()
            }}
            InputProps={{
              endAdornment: (
                <InputAdornment position="end">
                  <IconButton size="small">
                    <SearchIcon />
                  </IconButton>
                </InputAdornment>
              ),
            }}
          />
        </Box>
        <List dense sx={{ overflow: 'auto', flexGrow: 1 }}>
          {/* 首次加载骨架屏（替代空白） */}
          {isFetching && (sessions ?? []).length === 0 &&
            [0, 1, 2, 3, 4].map((i) => (
              <ListItem key={i} dense>
                <ListItemText
                  primary={<Skeleton variant="text" width="60%" height={22} />}
                  secondary={<Skeleton variant="text" width="40%" height={16} />}
                />
              </ListItem>
            ))}
          {(sessions ?? []).map((s) => (
            <ListItemButton
              key={s.username}
              selected={selected?.username === s.username}
              onClick={() => setSelected(s)}
            >
              <ListItemText
                primary={s.display_name}
                secondary={`${s.kind === 'group' ? '群聊' : '好友'} · ${s.username}`}
              />
            </ListItemButton>
          ))}
          {!isFetching && (sessions ?? []).length === 0 && (
            <ListItemButton disabled>
              <ListItemText primary="未找到会话" secondary="微信数据库不可用或无匹配结果" />
            </ListItemButton>
          )}
        </List>
      </Paper>

      {/* 右侧：消息流 */}
      <Paper variant="outlined" sx={{ flexGrow: 1, overflow: 'auto', p: 2 }}>
        {!selected && (
          <Typography color="text.secondary" align="center" sx={{ mt: 8 }}>
            在左侧选择一个会话查看消息
          </Typography>
        )}
        {selected && (
          <>
            <Typography variant="subtitle1" sx={{ mb: 1, position: 'sticky', top: 0 }}>
              {selected.display_name}
              <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                {selected.username}
              </Typography>
            </Typography>
            {(messages ?? [])
              .slice()
              .reverse()
              .map((m) => (
                <Box key={`${m.sort_seq}-${m.local_id}`} sx={{ mb: 1, borderBottom: '1px dashed #eee', pb: 0.5 }}>
                  <Typography variant="caption" color="text.secondary">
                    {m.create_time.replace('T', ' ')} · {m.sender_name} · {m.type_name}
                  </Typography>
                  <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                    {m.content}
                  </Typography>
                </Box>
              ))}
            {selected && (messages ?? []).length === 0 && (
              <Typography color="text.secondary">该会话暂无消息或读取失败</Typography>
            )}
          </>
        )}
      </Paper>
    </Box>
  )

  /** 会话搜索由 useQuery 的 queryKey 变化自动触发；此函数仅为回车提供焦点反馈。 */
  function refetchHint() {
    return Promise.resolve()
  }
}

import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { WsEvent } from '../api/types'

/**
 * WS 事件订阅（/ws/events）：断线自动重连 + react-query 失效联动。
 *
 * 事件处理：
 * - opportunity_new / opportunity_update / unread_cleared → 失效商机相关查询
 * - status_change → 失效状态查询（顶部横幅）
 * - send_result → 经 window CustomEvent("kefu-send-result") 分发给详情页
 * WS 不可用时页面自动退化为轮询刷新（react-query refetchInterval）。
 */
export function useEvents(): boolean {
  const queryClient = useQueryClient()
  const [connected, setConnected] = useState(false)
  const retryRef = useRef(0)

  useEffect(() => {
    let ws: WebSocket | null = null
    let timer: number | undefined
    let closed = false

    const connect = () => {
      if (closed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      try {
        ws = new WebSocket(`${proto}://${window.location.host}/ws/events`)
      } catch {
        scheduleReconnect()
        return
      }
      ws.onopen = () => {
        retryRef.current = 0
        setConnected(true)
        void queryClient.invalidateQueries()
      }
      ws.onmessage = (ev: MessageEvent) => {
        let msg: WsEvent
        try {
          msg = JSON.parse(String(ev.data)) as WsEvent
        } catch {
          return
        }
        switch (msg.event) {
          case 'opportunity_new':
            window.dispatchEvent(new CustomEvent('kefu-opportunity-new-sound'))
            void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
            void queryClient.invalidateQueries({ queryKey: ['status'] })
            break
          case 'opportunity_update':
          case 'unread_cleared':
            void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
            void queryClient.invalidateQueries({ queryKey: ['opportunity'] })
            break
          case 'status_change':
            void queryClient.invalidateQueries({ queryKey: ['status'] })
            break
          case 'send_result':
            window.dispatchEvent(
              new CustomEvent('kefu-send-result', { detail: msg.data }),
            )
            void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
            void queryClient.invalidateQueries({ queryKey: ['opportunity'] })
            break
          default:
            break
        }
      }
      ws.onclose = () => {
        setConnected(false)
        scheduleReconnect()
      }
      ws.onerror = () => {
        ws?.close()
      }
    }

    const scheduleReconnect = () => {
      if (closed) return
      const delay = Math.min(10000, 1000 * 2 ** retryRef.current)
      retryRef.current += 1
      timer = window.setTimeout(connect, delay)
    }

    connect()
    return () => {
      closed = true
      if (timer !== undefined) window.clearTimeout(timer)
      ws?.close()
    }
  }, [queryClient])

  return connected
}

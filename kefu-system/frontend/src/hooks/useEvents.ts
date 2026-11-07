import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { WsEvent } from '../api/types'

/**
 * WS 事件订阅（/ws/events）：断线自动重连 + react-query 失效联动。
 *
 * 事件处理：
 * - opportunity_new → 失效商机相关查询 + 派发 kefu-opportunity-new-sound（提示音）
 *   + 派发 kefu-opportunity-toast（App 全局右上角 toast，任何页面可见）
 * - opportunity_update / unread_cleared → 仅刷新商机列表（合并更新不弹提醒）
 * - analysis_error → 派发 kefu-analysis-error（AI 分析失败警告 toast）
 *   + 失效状态查询（连续失败 ≥3 次时顶部常驻横幅由 /api/status 驱动）
 * - status_change → 失效状态查询（顶部横幅）
 * - send_result → 经 window CustomEvent("kefu-send-result") 分发给详情页
 * WS 不可用时页面自动退化为轮询刷新（react-query refetchInterval）。
 */
interface OpportunityNewData {
  id?: number
  session_name?: string
  summary?: string
}

interface AnalysisErrorData {
  error?: string
  consecutive_failures?: number
}

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
          case 'opportunity_new': {
            const d = (msg.data ?? {}) as OpportunityNewData
            if (typeof d.id === 'number') {
              window.dispatchEvent(
                new CustomEvent('kefu-opportunity-toast', {
                  detail: {
                    id: d.id,
                    session_name: d.session_name ?? '',
                    summary: d.summary ?? '',
                  },
                }),
              )
            }
            window.dispatchEvent(new CustomEvent('kefu-opportunity-new-sound'))
            void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
            void queryClient.invalidateQueries({ queryKey: ['status'] })
            break
          }
          case 'analysis_error': {
            const d = (msg.data ?? {}) as AnalysisErrorData
            window.dispatchEvent(
              new CustomEvent('kefu-analysis-error', {
                detail: {
                  error: d.error ?? '未知错误',
                  consecutive_failures: d.consecutive_failures ?? 1,
                },
              }),
            )
            void queryClient.invalidateQueries({ queryKey: ['status'] })
            break
          }
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

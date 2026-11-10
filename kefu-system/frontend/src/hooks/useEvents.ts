import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { WsEvent } from '../api/types'

/**
 * WS 事件订阅（/ws/events）：断线自动重连 + react-query 失效联动。
 *
 * 事件处理：
 * - opportunity_new → 失效商机相关查询 + 派发 kefu-opportunity-new-sound（提示音）
 *   + 派发 kefu-opportunity-toast（App 全局右上角 toast，任何页面可见）
 *   + 页面不可见且已授权系统通知时发 Web Notification（点击回焦并打开详情抽屉）
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

/** 提取摘要首行非空文本（系统通知正文用） */
function firstLine(text: string | undefined): string {
  return (
    String(text ?? '')
      .split('\n')
      .map((l) => l.trim())
      .find(Boolean) ?? ''
  )
}

/** 系统通知应用级开关 localStorage 键（设置页 Switch 写入，"1"=开） */
export const SYSTEM_NOTIFY_KEY = 'kefu.system_notify_enabled'

/** 系统通知是否开启（应用级开关；需与浏览器授权同时满足才发通知） */
function isSystemNotifyEnabled(): boolean {
  try {
    return window.localStorage.getItem(SYSTEM_NOTIFY_KEY) === '1'
  } catch {
    return false
  }
}

/** 系统通知（应用开关开启 + 已授权 + 页面不可见时）：点击 → 回焦并打开详情。 */
function showSystemNotification(id: number, body: string): void {
  try {
    if (
      typeof Notification === 'undefined' ||
      !isSystemNotifyEnabled() ||
      Notification.permission !== 'granted'
    ) {
      return
    }
    const notification = new Notification('发现新商机', {
      body,
      tag: `kefu-opp-${id}`, // 同一商机只弹一个通知
    })
    notification.onclick = () => {
      window.focus()
      notification.close()
      // App 监听该事件：导航回商机列表页并打开详情抽屉（pending 兜底）
      window.dispatchEvent(
        new CustomEvent('kefu-notification-click', { detail: { id } }),
      )
    }
  } catch {
    // 通知失败静默降级（应用内 toast 已有兜底）
  }
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
            // 系统通知：仅页面不可见时发送，避免与应用内 toast 重复打扰
            if (document.hidden && typeof d.id === 'number') {
              showSystemNotification(
                d.id,
                [d.session_name ?? '', firstLine(d.summary)]
                  .filter(Boolean)
                  .join('：'),
              )
            }
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
            void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
            void queryClient.invalidateQueries({ queryKey: ['opportunity'] })
            break
          case 'unread_cleared':
            // 已读即时生效（反馈 3）：列表 + 未读角标一起刷新，
            // 不再等 /api/status 的 5 秒轮询
            void queryClient.invalidateQueries({ queryKey: ['opportunities'] })
            void queryClient.invalidateQueries({ queryKey: ['opportunity'] })
            void queryClient.invalidateQueries({ queryKey: ['status'] })
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

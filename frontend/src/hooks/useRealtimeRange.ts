// frontend/src/hooks/useRealtimeRange.ts
/**
 * React hook for real-time range updates via WebSocket.
 *
 * Provides:
 * - Automatic WebSocket connection management
 * - Subscription to range-specific events
 * - Automatic reconnection with exponential backoff
 * - Event callbacks for UI updates
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { api } from '../services/api'
import { useAuthStore } from '../stores/authStore'
import { RealtimeEvent, WebSocketConnectionState } from '../types'

const WS_BASE_URL = (import.meta as unknown as { env: Record<string, string> }).env?.VITE_WS_URL || `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`
const MAX_RECONNECT_ATTEMPTS = 5
const INITIAL_RECONNECT_DELAY = 1000

/** The sockets a ticket can be minted for, spelled as the API spells them. */
export type WebSocketTicketKind = 'console' | 'vnc' | 'range-console' | 'status' | 'events'

/**
 * Exchange the session for a short-lived cookie that opens ONE websocket.
 *
 * `new WebSocket(url)` takes a URL and nothing else, so the session JWT used to
 * travel in the query string -- which put a credential good for the whole API
 * into the browser's history, into the access log of every proxy and ingress
 * between here and the server, and into a Referer if the page ever linked out.
 * What comes back from this call is an HttpOnly cookie scoped to that one
 * socket's path; the browser sends it on the handshake by itself, so the URL
 * carries nothing worth stealing.
 *
 * Call it immediately before every connect, reconnects included: a ticket is
 * good for minutes, not for the life of the page.
 *
 * It lives in this module because four callers need it and a second copy of an
 * access decision is the kind that gets fixed in one place and stays broken in
 * the other. It belongs in a websocket service module once there is one.
 *
 * Rejects if the server refuses, so the caller can say why rather than opening
 * a socket that is about to be closed under it.
 */
export async function requestWebSocketTicket(
  kind: WebSocketTicketKind,
  resourceId?: string
): Promise<void> {
  await api.post('/ws/ticket', { kind, resource_id: resourceId ?? null })
}

/** Exponential backoff with jitter, so many clients do not reconnect in lockstep. */
function reconnectDelay(attempt: number): number {
  const baseDelay = Math.min(INITIAL_RECONNECT_DELAY * Math.pow(2, attempt), 30000)
  return Math.round(baseDelay + Math.random() * 0.25 * baseDelay)
}

interface UseRealtimeRangeOptions {
  onEvent?: (event: RealtimeEvent) => void
  onStatusChange?: (rangeStatus: string, vmStatuses: Record<string, string>) => void
  onVmStatusChange?: (vmId: string, status: string) => void
  onDeploymentProgress?: (step: string, message: string) => void
  onError?: (error: string) => void
  enabled?: boolean
}

interface UseRealtimeRangeReturn {
  connectionState: WebSocketConnectionState
  lastEvent: RealtimeEvent | null
  subscribe: (rangeId: string) => void
  unsubscribe: (rangeId: string) => void
  subscribeToVm: (vmId: string) => void
}

export function useRealtimeRange(
  rangeId: string | null,
  options: UseRealtimeRangeOptions = {}
): UseRealtimeRangeReturn {
  const {
    onEvent,
    onStatusChange,
    onVmStatusChange,
    onDeploymentProgress,
    onError,
    enabled = true,
  } = options

  const token = useAuthStore((state) => state.token)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectAttemptsRef = useRef(0)
  const mountedRef = useRef(true)

  const [connectionState, setConnectionState] = useState<WebSocketConnectionState>('disconnected')
  const [lastEvent, setLastEvent] = useState<RealtimeEvent | null>(null)

  // Callback refs to avoid reconnection on callback changes
  const callbacksRef = useRef({ onEvent, onStatusChange, onVmStatusChange, onDeploymentProgress, onError })
  useEffect(() => {
    callbacksRef.current = { onEvent, onStatusChange, onVmStatusChange, onDeploymentProgress, onError }
  }, [onEvent, onStatusChange, onVmStatusChange, onDeploymentProgress, onError])

  // A scheduled retry has to call the *current* `connect`, and `connect`
  // cannot name itself inside its own initializer. The ref is that
  // indirection, kept in step by the effect below.
  const connectRef = useRef<() => Promise<void>>(() => Promise.resolve())

  const connect: () => Promise<void> = useCallback(async () => {
    if (!token || !rangeId || !enabled) return

    // Prevent rapid reconnection - wait for previous close to complete
    if (wsRef.current && wsRef.current.readyState === WebSocket.CONNECTING) {
      console.log('[WebSocket] Connection already in progress, skipping')
      return
    }

    // Close existing connection cleanly
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.close(1000, 'Reconnecting')
      wsRef.current = null
    }

    setConnectionState('connecting')

    // The credential is a cookie the API sets, not a query parameter. Minted
    // here rather than once per page because it is deliberately short-lived.
    try {
      await requestWebSocketTicket('events')
    } catch (err) {
      console.error('[WebSocket] Ticket refused:', err)
      if (!mountedRef.current) return
      setConnectionState('error')
      callbacksRef.current.onError?.('Could not obtain a websocket ticket')
      // Treated as a failed connection rather than a dead end: a refused mint
      // is usually a blip or an expired session, and giving up silently leaves
      // the range page showing stale state with nothing saying why.
      if (reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS && enabled) {
        reconnectTimeoutRef.current = setTimeout(() => {
          if (mountedRef.current) {
            reconnectAttemptsRef.current++
            void connectRef.current()
          }
        }, reconnectDelay(reconnectAttemptsRef.current))
      }
      return
    }

    // The effect was torn down while the ticket was in flight.
    if (!mountedRef.current) return

    // No token in the URL: see requestWebSocketTicket.
    const wsUrl = `${WS_BASE_URL}/api/v1/ws/events?range_id=${encodeURIComponent(rangeId)}`

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      console.log('[WebSocket] Connected to real-time events')
      setConnectionState('connected')
      reconnectAttemptsRef.current = 0
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)

        // Handle different message types
        if (data.type === 'ping') {
          // Respond to keepalive ping
          ws.send(JSON.stringify({ action: 'ping' }))
          return
        }

        if (data.type === 'connected') {
          console.log('[WebSocket] Subscription confirmed:', data.subscriptions)
          return
        }

        if (data.type === 'status_update') {
          // Handle status update from /ws/status endpoint format
          callbacksRef.current.onStatusChange?.(data.range_status, data.vms || {})
          return
        }

        // Handle real-time event
        if (data.event_type) {
          const realtimeEvent: RealtimeEvent = {
            event_type: data.event_type,
            range_id: data.range_id,
            vm_id: data.vm_id,
            message: data.message,
            data: data.data,
            timestamp: data.timestamp,
          }

          setLastEvent(realtimeEvent)
          callbacksRef.current.onEvent?.(realtimeEvent)

          // Route to specific handlers based on event type
          if (data.event_type.startsWith('vm_') || data.event_type === 'vm.status_changed') {
            const vmStatus = data.data?.status as string
            if (data.vm_id && vmStatus) {
              callbacksRef.current.onVmStatusChange?.(data.vm_id, vmStatus)
            }
          }

          if (data.event_type.startsWith('deployment_') || data.event_type === 'deployment_step') {
            callbacksRef.current.onDeploymentProgress?.(
              data.data?.step as string || data.event_type,
              data.message
            )
          }
        }
      } catch (err) {
        console.error('[WebSocket] Failed to parse message:', err)
      }
    }

    ws.onerror = (error) => {
      console.error('[WebSocket] Error:', error)
      setConnectionState('error')
      callbacksRef.current.onError?.('WebSocket connection error')
    }

    ws.onclose = (event) => {
      console.log('[WebSocket] Disconnected:', event.code, event.reason)
      setConnectionState('disconnected')
      wsRef.current = null

      // Don't reconnect if unmounted or clean close
      if (!mountedRef.current) return

      // Attempt reconnection if not a clean close and we haven't exceeded attempts
      if (event.code !== 1000 && reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS && enabled) {
        const delay = reconnectDelay(reconnectAttemptsRef.current)
        console.log(`[WebSocket] Reconnecting in ${delay}ms (attempt ${reconnectAttemptsRef.current + 1})`)

        reconnectTimeoutRef.current = setTimeout(() => {
          if (mountedRef.current) {
            reconnectAttemptsRef.current++
            void connectRef.current()
          }
        }, delay)
      }
    }
  }, [token, rangeId, enabled])

  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  // Subscribe to additional range
  const subscribe = useCallback((targetRangeId: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'subscribe',
        range_id: targetRangeId,
      }))
    }
  }, [])

  // Unsubscribe from range
  const unsubscribe = useCallback((targetRangeId: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'unsubscribe',
        range_id: targetRangeId,
      }))
    }
  }, [])

  // Subscribe to VM events
  const subscribeToVm = useCallback((vmId: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'subscribe_vm',
        vm_id: vmId,
      }))
    }
  }, [])

  // Connect on mount and when dependencies change
  useEffect(() => {
    mountedRef.current = true
    let connectTimeout: ReturnType<typeof setTimeout> | null = null

    if (enabled && rangeId && token) {
      // Small delay to avoid React 18 Strict Mode double-render race condition
      connectTimeout = setTimeout(() => {
        if (mountedRef.current) {
          void connect()
        }
      }, 100)
    }

    return () => {
      // Cleanup on unmount
      mountedRef.current = false
      if (connectTimeout) {
        clearTimeout(connectTimeout)
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      if (wsRef.current) {
        wsRef.current.close(1000, 'Component unmounted')
        wsRef.current = null
      }
    }
  }, [connect, enabled, rangeId, token])

  return {
    connectionState,
    lastEvent,
    subscribe,
    unsubscribe,
    subscribeToVm,
  }
}

/**
 * Hook for listening to system-wide events (not range-specific).
 * Useful for dashboard-level notifications.
 */
export function useRealtimeEvents(options: Omit<UseRealtimeRangeOptions, 'onStatusChange'> = {}) {
  return useRealtimeRange(null, {
    ...options,
    enabled: options.enabled ?? true,
  })
}

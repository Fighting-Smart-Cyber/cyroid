// frontend/src/hooks/useGlobalNotifications.ts
/**
 * Hook for subscribing to global system notifications via WebSocket.
 * Connects without a range_id to receive all system events.
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'
import { useNotificationStore } from '../stores/notificationStore'
import { requestWebSocketTicket } from './useRealtimeRange'
import { RealtimeEvent } from '../types'

const WS_BASE_URL = (import.meta as unknown as { env: Record<string, string> }).env?.VITE_WS_URL || `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`
const MAX_RECONNECT_ATTEMPTS = 10
const INITIAL_RECONNECT_DELAY = 1000

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error'

interface UseGlobalNotificationsOptions {
  enabled?: boolean
  onNotification?: (event: RealtimeEvent) => void
}

interface UseGlobalNotificationsReturn {
  connectionState: ConnectionState
  isConnected: boolean
}

export function useGlobalNotifications(
  options: UseGlobalNotificationsOptions = {}
): UseGlobalNotificationsReturn {
  const { enabled = true, onNotification } = options

  const token = useAuthStore((state) => state.token)
  const addNotification = useNotificationStore((state) => state.addNotification)

  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectAttemptsRef = useRef(0)
  const mountedRef = useRef(true)

  const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected')

  // Callback ref to avoid reconnection on callback changes
  const onNotificationRef = useRef(onNotification)
  useEffect(() => {
    onNotificationRef.current = onNotification
  }, [onNotification])

  // A scheduled retry has to call the *current* `connect`, and `connect`
  // cannot name itself inside its own initializer. The ref is that
  // indirection, kept in step by the effect below.
  const connectRef = useRef<() => Promise<void>>(() => Promise.resolve())

  const connect: () => Promise<void> = useCallback(async () => {
    if (!token || !enabled) return

    // Prevent rapid reconnection - wait for previous close to complete
    if (wsRef.current && wsRef.current.readyState === WebSocket.CONNECTING) {
      console.log('[GlobalNotifications] Connection already in progress, skipping')
      return
    }

    // Close existing connection cleanly
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.close(1000, 'Reconnecting')
      wsRef.current = null
    }

    setConnectionState('connecting')

    // The credential is a cookie the API sets, not a query parameter -- see
    // requestWebSocketTicket. Minted on every attempt because it is short-lived.
    try {
      await requestWebSocketTicket('events')
    } catch (err) {
      console.error('[GlobalNotifications] Ticket refused:', err)
      if (!mountedRef.current) return
      setConnectionState('error')
      // Treated as a failed connection rather than a dead end: a refused mint
      // is usually a blip or an expired session, and the notification bell
      // going quiet for the rest of the session is not an acceptable answer.
      if (reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS && enabled) {
        const baseDelay = Math.min(
          INITIAL_RECONNECT_DELAY * Math.pow(2, reconnectAttemptsRef.current),
          30000
        )
        const delay = Math.round(baseDelay + Math.random() * 0.25 * baseDelay)
        reconnectTimeoutRef.current = setTimeout(() => {
          if (mountedRef.current) {
            reconnectAttemptsRef.current++
            void connectRef.current()
          }
        }, delay)
      }
      return
    }

    // The hook was unmounted while the ticket was in flight.
    if (!mountedRef.current) return

    // Connect without range_id to get all global events
    const wsUrl = `${WS_BASE_URL}/api/v1/ws/events`

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      console.log('[GlobalNotifications] Connected')
      setConnectionState('connected')
      reconnectAttemptsRef.current = 0
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)

        // Handle keepalive ping
        if (data.type === 'ping') {
          ws.send(JSON.stringify({ action: 'ping' }))
          return
        }

        // Handle connection confirmation
        if (data.type === 'connected') {
          console.log('[GlobalNotifications] Subscription confirmed')
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

          // Add to notification store
          addNotification(realtimeEvent)

          // Call optional callback (for toast triggering)
          onNotificationRef.current?.(realtimeEvent)
        }
      } catch (err) {
        console.error('[GlobalNotifications] Failed to parse message:', err)
      }
    }

    ws.onerror = (error) => {
      console.error('[GlobalNotifications] Error:', error)
      setConnectionState('error')
    }

    ws.onclose = (event) => {
      console.log('[GlobalNotifications] Disconnected:', event.code, event.reason)
      setConnectionState('disconnected')
      wsRef.current = null

      // Don't reconnect if unmounted or clean close
      if (!mountedRef.current) return

      // Attempt reconnection if not a clean close
      if (event.code !== 1000 && reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS && enabled) {
        const baseDelay = Math.min(
          INITIAL_RECONNECT_DELAY * Math.pow(2, reconnectAttemptsRef.current),
          30000 // Max 30 seconds
        )
        // Add random jitter (0-25%) to prevent thundering herd when many clients reconnect
        const jitter = Math.random() * 0.25 * baseDelay
        const delay = Math.round(baseDelay + jitter)
        console.log(`[GlobalNotifications] Reconnecting in ${delay}ms (attempt ${reconnectAttemptsRef.current + 1})`)

        reconnectTimeoutRef.current = setTimeout(() => {
          if (mountedRef.current) {
            reconnectAttemptsRef.current++
            void connectRef.current()
          }
        }, delay)
      }
    }
  }, [token, enabled, addNotification])

  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  // Connect on mount and when dependencies change
  useEffect(() => {
    mountedRef.current = true
    let connectTimeout: ReturnType<typeof setTimeout> | null = null

    if (enabled && token) {
      // Small delay to avoid React 18 Strict Mode double-render race condition
      // This ensures the first mount/unmount cycle completes before we connect
      connectTimeout = setTimeout(() => {
        if (mountedRef.current) {
          void connect()
        }
      }, 100)
    }

    return () => {
      mountedRef.current = false
      if (connectTimeout) {
        clearTimeout(connectTimeout)
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current)
      }
      if (wsRef.current) {
        wsRef.current.close(1000, 'Hook cleanup')
        wsRef.current = null
      }
    }
  }, [connect, enabled, token])

  return {
    connectionState,
    isConnected: connectionState === 'connected',
  }
}

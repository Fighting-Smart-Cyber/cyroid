// frontend/src/components/console/RangeConsole.tsx
/**
 * A shell inside the Docker host a range runs on, and the diagnostics that only exist there.
 *
 * Everything below -- the websocket, the six quick actions, the help -- talks to the range's
 * DinD container. On the Kubernetes substrate there is no such container: the websocket closes
 * 4000 and every action answers 400, which the console rendered as a connection timeout and an
 * error inside the terminal, as though the range were broken rather than the console being the
 * wrong tool. There it says so instead of connecting -- hiding the button that opens it does not
 * make this component unreachable.
 */
import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import {
  Maximize2, Minimize2, X, RefreshCw, Terminal as TerminalIcon,
  AlertTriangle, Play, Network, HardDrive, Route, Shield, ArrowRightLeft
} from 'lucide-react'
import clsx from 'clsx'
import { rangesApi } from '../../services/api'
import { requestWebSocketTicket } from '../../hooks/useRealtimeRange'
import { useCapabilitiesStore } from '../../stores/capabilitiesStore'

interface RangeConsoleProps {
  rangeId: string
  rangeName: string
  /**
   * The session, kept as a prop so that a sign-out or a token refresh
   * reconnects the console. It is no longer put in the socket URL: the
   * handshake carries an HttpOnly ticket cookie instead. See
   * requestWebSocketTicket.
   */
  token: string
  onClose?: () => void
}

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

const CONNECTION_TIMEOUT_MS = 15000

export function RangeConsole({ rangeId, rangeName, token, onClose }: RangeConsoleProps) {
  const terminalRef = useRef<HTMLDivElement>(null)
  const terminalInstance = useRef<Terminal | null>(null)
  const fitAddon = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Bumped on every effect run AND on every cleanup. Fetching the ticket is
  // asynchronous, so the terminal this connect was started for can be disposed
  // while the request is in flight -- a fast unmount, or React 18's double
  // mount in development. Without the check the connect writes to a destroyed
  // terminal and leaves a socket nobody will ever close.
  const generationRef = useRef(0)
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting')
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showHelp, setShowHelp] = useState(false)
  const [reconnectCount, setReconnectCount] = useState(0)
  const [quickActionLoading, setQuickActionLoading] = useState<string | null>(null)

  // Null until /system/capabilities answers. Connecting on an unknown substrate is what produced
  // the misleading timeout, so the socket waits for the answer.
  const features = useCapabilitiesStore((s) => s.features)
  const isDockerHost = features?.legacy_range_console === true

  useEffect(() => {
    if (!isDockerHost || !terminalRef.current) return

    const terminal = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      theme: {
        background: '#1a1a2e',
        foreground: '#eee',
        cursor: '#00d9ff',
        selectionBackground: '#264f78',
      },
    })

    const fit = new FitAddon()
    terminal.loadAddon(fit)
    terminal.open(terminalRef.current)
    fit.fit()

    terminalInstance.current = terminal
    fitAddon.current = fit

    generationRef.current += 1
    void connectWebSocket(terminal, generationRef.current)

    const handleResize = () => {
      fit.fit()
    }
    window.addEventListener('resize', handleResize)

    return () => {
      generationRef.current += 1
      window.removeEventListener('resize', handleResize)
      terminal.dispose()
      if (wsRef.current) {
        wsRef.current.close()
      }
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
    }
  }, [rangeId, token, reconnectCount, isDockerHost])

  const connectWebSocket = async (terminal: Terminal, generation: number) => {
    setConnectionStatus('connecting')
    setError(null)

    timeoutRef.current = setTimeout(() => {
      if (connectionStatus === 'connecting') {
        setConnectionStatus('error')
        setError('Connection timeout - DinD container may not be running')
        terminal.writeln('\r\n\x1b[31mConnection timeout\x1b[0m')
        terminal.writeln('\x1b[33mThe DinD container may not be running.\x1b[0m')
        terminal.writeln('\x1b[33mTry deploying or starting the range first.\x1b[0m')
      }
    }, CONNECTION_TIMEOUT_MS)

    terminal.writeln('\x1b[90mConnecting to range console...\x1b[0m')
    terminal.writeln('\x1b[90mRange: ' + rangeName + '\x1b[0m')

    // The session is not put in the socket URL. This trades it for an HttpOnly
    // cookie scoped to this one range console's path, which the handshake
    // sends by itself -- see requestWebSocketTicket.
    try {
      await requestWebSocketTicket('range-console', rangeId)
    } catch {
      if (generationRef.current !== generation) return
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
      setConnectionStatus('error')
      setError('Range console ticket refused')
      terminal.writeln('\r\n\x1b[31mRange console ticket refused\x1b[0m')
      terminal.writeln('\x1b[33mThis console is administrator-only, and the session may have expired.\x1b[0m')
      terminal.writeln('\x1b[33mSign in again, then use Reconnect.\x1b[0m')
      return
    }

    // The terminal was disposed while the ticket was in flight.
    if (generationRef.current !== generation) return

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const wsUrl = `${wsProtocol}//${window.location.host}/api/v1/ws/range-console/${rangeId}`

    const ws = new WebSocket(wsUrl)

    ws.onopen = () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
      setConnectionStatus('connected')
      setError(null)
      terminal.writeln('\x1b[32mConnected to DinD container\x1b[0m')
      terminal.writeln('\x1b[90mYou now have shell access to the range\'s Docker environment.\x1b[0m')
      terminal.writeln('\x1b[90mTry: docker ps, docker network ls, ps aux | grep socat\x1b[0m')
      terminal.writeln('')
    }

    ws.onmessage = (event) => {
      terminal.write(event.data)
    }

    ws.onerror = () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
      setConnectionStatus('error')
      setError('WebSocket connection failed')
      terminal.writeln('\r\n\x1b[31mConnection error\x1b[0m')
      terminal.writeln('\x1b[33mPossible causes:\x1b[0m')
      terminal.writeln('\x1b[33m  - Range is not deployed\x1b[0m')
      terminal.writeln('\x1b[33m  - DinD container is not running\x1b[0m')
      terminal.writeln('\x1b[33m  - Insufficient permissions\x1b[0m')
    }

    ws.onclose = (event) => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
      setConnectionStatus('disconnected')
      terminal.writeln('\r\n\x1b[33mConnection closed\x1b[0m')
      if (event.reason) {
        setError(event.reason)
        terminal.writeln('\x1b[90mReason: ' + event.reason + '\x1b[0m')
      }
    }

    terminal.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(data)
      }
    })

    wsRef.current = ws
  }

  const reconnect = () => {
    if (wsRef.current) {
      wsRef.current.close()
    }
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current)
    }
    if (terminalInstance.current) {
      terminalInstance.current.clear()
    }
    setReconnectCount(prev => prev + 1)
  }

  const toggleFullscreen = () => {
    setIsFullscreen(!isFullscreen)
    setTimeout(() => {
      fitAddon.current?.fit()
    }, 100)
  }

  // Quick action handlers
  const runQuickAction = async (action: string, displayName: string) => {
    setQuickActionLoading(action)
    const terminal = terminalInstance.current
    if (!terminal) return

    try {
      terminal.writeln(`\r\n\x1b[36m━━━ ${displayName} ━━━\x1b[0m`)

      let response
      switch (action) {
        case 'docker-ps':
          response = await rangesApi.getConsoleContainers(rangeId)
          terminal.writeln('\x1b[90mCONTAINER ID   NAME                    STATUS      IMAGE\x1b[0m')
          response.data.forEach((c: { id: string; name: string; status: string; image: string }) => {
            const status = c.status === 'running' ? '\x1b[32m' + c.status + '\x1b[0m' : '\x1b[31m' + c.status + '\x1b[0m'
            terminal.writeln(`${c.id.padEnd(14)} ${c.name.padEnd(24)} ${status.padEnd(20)} ${c.image}`)
          })
          break

        case 'docker-network':
          response = await rangesApi.getConsoleNetworks(rangeId)
          terminal.writeln('\x1b[90mNETWORK ID     NAME                    DRIVER      SCOPE\x1b[0m')
          response.data.forEach((n: { id: string; name: string; driver: string; scope: string }) => {
            terminal.writeln(`${n.id.padEnd(14)} ${n.name.padEnd(24)} ${n.driver.padEnd(12)} ${n.scope}`)
          })
          break

        case 'iptables':
          response = await rangesApi.getConsoleIptables(rangeId)
          terminal.writeln(response.data.iptables_nat)
          break

        case 'port-forwarding':
          response = await rangesApi.getConsolePortForwarding(rangeId)
          terminal.writeln(response.data.port_forwarding)
          break

        case 'routes':
          response = await rangesApi.getConsoleRoutes(rangeId)
          terminal.writeln(response.data.routes)
          break

        case 'stats':
          response = await rangesApi.getConsoleStats(rangeId)
          terminal.writeln(`Containers: ${response.data.container_count}`)
          terminal.writeln(`Networks: ${response.data.network_count}`)
          break
      }

      terminal.writeln('')
    } catch (err: unknown) {
      const errorMessage = err instanceof Error ? err.message : 'Unknown error'
      terminal.writeln(`\x1b[31mError: ${errorMessage}\x1b[0m`)
    } finally {
      setQuickActionLoading(null)
    }
  }

  const getStatusColor = () => {
    switch (connectionStatus) {
      case 'connected': return 'bg-green-500'
      case 'connecting': return 'bg-yellow-500 animate-pulse'
      case 'disconnected': return 'bg-gray-500'
      case 'error': return 'bg-red-500'
    }
  }

  const getStatusText = () => {
    switch (connectionStatus) {
      case 'connected': return 'Connected'
      case 'connecting': return 'Connecting...'
      case 'disconnected': return 'Disconnected'
      case 'error': return 'Error'
    }
  }

  if (!isDockerHost) {
    return (
      <div className="flex flex-col items-start gap-2 h-full bg-gray-900 rounded-lg p-6 text-sm">
        <div className="flex items-center gap-2 text-gray-200">
          <AlertTriangle className="w-4 h-4 text-yellow-500" />
          <span className="font-medium">
            {features ? 'There is no range console on this install' : 'Checking this install...'}
          </span>
        </div>
        {features && (
          <>
            <p className="text-gray-400">
              A range here is placed in a namespace on the cluster. There is no host underneath it
              to open a shell on, so this console has nothing to connect to.
            </p>
            <p className="text-gray-400">
              Open a machine&apos;s console for a session on that machine, and Diagnostics for what
              the range consists of on the cluster.
            </p>
          </>
        )}
        {onClose && (
          <button
            onClick={onClose}
            className="mt-2 px-3 py-1.5 text-xs text-gray-200 bg-gray-700 hover:bg-gray-600 rounded"
          >
            Close
          </button>
        )}
      </div>
    )
  }

  return (
    <div
      className={clsx(
        'flex flex-col bg-gray-900 rounded-lg overflow-hidden shadow-xl',
        isFullscreen ? 'fixed inset-4 z-50' : 'h-full'
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 bg-gray-800 border-b border-gray-700">
        <div className="flex items-center gap-2">
          <TerminalIcon className="w-4 h-4 text-cyan-400" />
          <div className={clsx('w-2 h-2 rounded-full', getStatusColor())} />
          <span className="text-sm font-medium text-gray-200">
            Range Console
          </span>
          <span className="text-xs text-gray-400">
            {getStatusText()}
          </span>
          {error && (
            <span className="text-xs text-red-400 ml-2">{error}</span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setShowHelp(!showHelp)}
            className={clsx(
              "p-1.5 hover:bg-gray-700 rounded",
              showHelp ? "text-blue-400" : "text-gray-400 hover:text-white"
            )}
            title="Help"
          >
            <AlertTriangle className="w-4 h-4" />
          </button>
          <button
            onClick={reconnect}
            className="p-1.5 text-gray-400 hover:text-white hover:bg-gray-700 rounded"
            title="Reconnect"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
          <button
            onClick={toggleFullscreen}
            className="p-1.5 text-gray-400 hover:text-white hover:bg-gray-700 rounded"
            title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          >
            {isFullscreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="p-1.5 text-gray-400 hover:text-white hover:bg-gray-700 rounded"
              title="Close"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Quick Actions */}
      <div className="flex items-center gap-2 px-3 py-2 bg-gray-800/50 border-b border-gray-700">
        <span className="text-xs text-gray-500 mr-1">Quick Actions:</span>
        <button
          onClick={() => runQuickAction('docker-ps', 'Container List')}
          disabled={quickActionLoading !== null}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded disabled:opacity-50"
        >
          <HardDrive className="w-3 h-3" />
          {quickActionLoading === 'docker-ps' ? 'Loading...' : 'Containers'}
        </button>
        <button
          onClick={() => runQuickAction('docker-network', 'Network List')}
          disabled={quickActionLoading !== null}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded disabled:opacity-50"
        >
          <Network className="w-3 h-3" />
          {quickActionLoading === 'docker-network' ? 'Loading...' : 'Networks'}
        </button>
        <button
          onClick={() => runQuickAction('iptables', 'Network Isolation')}
          disabled={quickActionLoading !== null}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded disabled:opacity-50"
        >
          <Shield className="w-3 h-3" />
          {quickActionLoading === 'iptables' ? 'Loading...' : 'Net Isolation'}
        </button>
        <button
          onClick={() => runQuickAction('port-forwarding', 'Port Forwarding')}
          disabled={quickActionLoading !== null}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded disabled:opacity-50"
        >
          <ArrowRightLeft className="w-3 h-3" />
          {quickActionLoading === 'port-forwarding' ? 'Loading...' : 'Port Fwd'}
        </button>
        <button
          onClick={() => runQuickAction('routes', 'IP Routes')}
          disabled={quickActionLoading !== null}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded disabled:opacity-50"
        >
          <Route className="w-3 h-3" />
          {quickActionLoading === 'routes' ? 'Loading...' : 'Routes'}
        </button>
        <button
          onClick={() => runQuickAction('stats', 'Resource Stats')}
          disabled={quickActionLoading !== null}
          className="inline-flex items-center gap-1 px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 rounded disabled:opacity-50"
        >
          <Play className="w-3 h-3" />
          {quickActionLoading === 'stats' ? 'Loading...' : 'Stats'}
        </button>
      </div>

      {/* Help panel */}
      {showHelp && (
        <div className="px-4 py-3 bg-gray-800/50 border-b border-gray-700 text-sm">
          <p className="text-gray-300 font-medium mb-2">Range Console Help</p>
          <ul className="text-gray-400 space-y-1 text-xs">
            <li>• This console provides shell access to the <strong>DinD container</strong> hosting your range.</li>
            <li>• Run <code className="bg-gray-700 px-1 rounded">docker ps</code> to see all VMs/containers in this range.</li>
            <li>• Run <code className="bg-gray-700 px-1 rounded">docker logs &lt;container&gt;</code> to view VM logs.</li>
            <li>• Run <code className="bg-gray-700 px-1 rounded">docker network ls</code> to see range networks.</li>
            <li>• Run <code className="bg-gray-700 px-1 rounded">ps aux | grep socat</code> to see VNC port forwarding proxies.</li>
            <li>• Run <code className="bg-gray-700 px-1 rounded">iptables -L FORWARD</code> to see network isolation rules.</li>
            <li>• <strong>Quick Actions</strong> provide one-click access to common diagnostic commands.</li>
          </ul>
        </div>
      )}

      {/* Terminal */}
      <div ref={terminalRef} className="flex-1 p-2" />
    </div>
  )
}

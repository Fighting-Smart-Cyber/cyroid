/**
 * Console for a KubeVirt workload -- COSMOS PG-61.
 *
 * A container console embeds the VM image's own KasmVNC page in an iframe. A KubeVirt VM has no
 * such page: its VNC framebuffer comes off the launcher pod through the API server, and the API
 * pipes it to us over /ws/vnc/k8s/{range}/{workload}. So the client lives here, in the app: noVNC
 * drawing into a canvas, with the same look and the same buttons as the container console.
 *
 * Clipboard: text the VM puts on its clipboard arrives as an RFB event and is copied to the
 * browser's; "Paste" sends the browser's clipboard into the VM as RFB client cut text. Whether
 * the guest honours it depends on the guest (QEMU's VNC clipboard needs an agent in the guest);
 * the wiring here is the same either way.
 *
 * The socket URL used to carry the session token, because a browser cannot set headers on a
 * websocket. A URL is written to every proxy log it passes and kept in the browser's history, so
 * that left a credential good for the whole API in both. The session is now traded for a ticket
 * scoped to this one machine, which comes back as an HttpOnly cookie the handshake sends by
 * itself -- so the URL carries nothing at all. A fresh one is minted on every open and every
 * reconnect; nothing here holds one.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc/lib/rfb'
import { Clipboard, Keyboard, Maximize2, RefreshCw } from 'lucide-react'

type Status = 'connecting' | 'connected' | 'disconnected' | 'error'

interface Props {
  rangeId: string
  workload: string
  /** Fill the window (the pop-out page) rather than a fixed-height panel. */
  fullscreen?: boolean
}

/**
 * Trade the session for a ticket that opens this one machine's console socket, and return the
 * path to dial.
 *
 * `credentials: 'same-origin'` is load-bearing in both directions: it is what lets the browser
 * store the ticket cookie the response sets, and the handshake is what sends it back. The path
 * comes from the server rather than being built here, because the cookie is scoped to the path
 * the server wrote -- a name normalised differently on this side would dial somewhere the
 * cookie is not offered, and the socket would refuse a ticket the browser is holding.
 *
 * Throws with whatever the API said, because the reasons are ones the person can act on: the
 * range is not theirs, the machine was hidden from them for this exercise, or it is not a
 * machine this range declares. A bare "connection failed" hides all three.
 */
async function requestConsolePath(rangeId: string, workload: string, token: string): Promise<string> {
  const res = await fetch(
    `/api/v1/ranges/${rangeId}/workloads/${encodeURIComponent(workload)}/console-ticket`,
    { method: 'POST', headers: { Authorization: `Bearer ${token}` }, credentials: 'same-origin' }
  )
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null
    throw new Error(
      typeof body?.detail === 'string' ? body.detail : 'This console is not available to you'
    )
  }
  const { path } = (await res.json()) as { path: string }
  return path
}

export function KubeVirtConsole({ rangeId, workload, fullscreen = false }: Props) {
  const canvasHost = useRef<HTMLDivElement>(null)
  const rfbRef = useRef<RFB | null>(null)
  const [status, setStatus] = useState<Status>('connecting')
  const [detail, setDetail] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [clipboardNote, setClipboardNote] = useState<string | null>(null)

  useEffect(() => {
    const host = canvasHost.current
    const token = localStorage.getItem('token') || ''
    if (!host || !token) {
      setStatus('error')
      setDetail(token ? 'No canvas to draw into' : 'Not authenticated')
      return
    }
    setStatus('connecting')
    setDetail(null)

    // The ticket round trip means the socket is opened a moment after this effect runs, so the
    // component can be torn down (or re-run for another machine) while the request is in flight.
    // Without this flag that late RFB would attach to a host div the next effect already owns,
    // leaving two clients drawing into one canvas and neither one disconnected on unmount.
    let cancelled = false
    let rfb: RFB | null = null

    const connect = async () => {
      const path = await requestConsolePath(rangeId, workload, token).catch((err: unknown) => {
        if (!cancelled) {
          setStatus('error')
          setDetail(err instanceof Error ? err.message : 'Could not open this console')
        }
        return null
      })
      if (path === null || cancelled) return

      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const url = `${wsProtocol}//${window.location.host}${path}`

      // The API accepts the socket with subprotocol "binary"; noVNC must offer the same one or the
      // browser refuses the upgrade as a protocol mismatch.
      rfb = new RFB(host, url, { wsProtocols: ['binary'] })
      rfb.scaleViewport = true
      rfb.resizeSession = false
      rfb.background = '#111827'
      rfbRef.current = rfb

      rfb.addEventListener('connect', () => setStatus('connected'))
      rfb.addEventListener('disconnect', (e) => {
        setStatus(e.detail.clean ? 'disconnected' : 'error')
        if (!e.detail.clean) setDetail((d) => d ?? 'The connection dropped')
      })
      rfb.addEventListener('securityfailure', (e) => {
        setStatus('error')
        setDetail(e.detail.reason ?? 'VNC security failure')
      })
      rfb.addEventListener('clipboard', (e) => {
        // Text the VM copied: mirror it to the browser's clipboard, where the API allows.
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(e.detail.text).then(
            () => setClipboardNote('Copied from VM'),
            () => setClipboardNote('VM clipboard received; browser refused to store it')
          )
          window.setTimeout(() => setClipboardNote(null), 2500)
        }
      })
    }
    void connect()

    return () => {
      cancelled = true
      rfbRef.current = null
      rfb?.disconnect()
    }
  }, [rangeId, workload, attempt])

  const pasteIntoVm = useCallback(async () => {
    const rfb = rfbRef.current
    if (!rfb || !navigator.clipboard?.readText) return
    try {
      const text = await navigator.clipboard.readText()
      if (text) {
        rfb.clipboardPasteFrom(text)
        setClipboardNote('Sent to VM clipboard')
      } else {
        setClipboardNote('Browser clipboard is empty')
      }
    } catch {
      setClipboardNote('Browser refused clipboard access')
    }
    window.setTimeout(() => setClipboardNote(null), 2500)
  }, [])

  const statusText: Record<Status, string> = {
    connecting: 'Connecting…',
    connected: 'Connected',
    disconnected: 'Disconnected',
    error: detail ?? 'Error',
  }
  const statusColour: Record<Status, string> = {
    connecting: 'text-yellow-300',
    connected: 'text-green-400',
    disconnected: 'text-gray-400',
    error: 'text-red-400',
  }

  return (
    <div className={`flex flex-col bg-gray-900 ${fullscreen ? 'h-screen' : 'h-[600px] rounded-lg overflow-hidden'}`}>
      <div className="flex items-center justify-between px-3 py-2 bg-gray-800 text-sm text-gray-200">
        <div className="flex items-center gap-3">
          <span className="font-medium">{workload}</span>
          <span className={statusColour[status]}>{statusText[status]}</span>
          {clipboardNote && <span className="text-gray-400">{clipboardNote}</span>}
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={pasteIntoVm}
            disabled={status !== 'connected'}
            title="Send the browser clipboard to the VM"
            className="p-1.5 rounded hover:bg-gray-700 disabled:opacity-40"
          >
            <Clipboard className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => rfbRef.current?.sendCtrlAltDel()}
            disabled={status !== 'connected'}
            title="Send Ctrl+Alt+Del"
            className="p-1.5 rounded hover:bg-gray-700 disabled:opacity-40"
          >
            <Keyboard className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => canvasHost.current?.requestFullscreen?.()}
            title="Fullscreen"
            className="p-1.5 rounded hover:bg-gray-700"
          >
            <Maximize2 className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => setAttempt((n) => n + 1)}
            title="Reconnect"
            className="p-1.5 rounded hover:bg-gray-700"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </div>
      <div ref={canvasHost} className="flex-1 min-h-0" />
    </div>
  )
}

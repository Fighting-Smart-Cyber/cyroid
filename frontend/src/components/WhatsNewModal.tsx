import { useEffect, useState } from 'react'
import { Sparkles, ArrowUpCircle, Bug } from 'lucide-react'
import Modal, { ModalBody, ModalFooter } from './common/Modal'
import { userFacingReleases, releasesSince, type Release, type ChangeKind } from '../lib/changelog'

const SEEN_KEY = 'pg:whatsNewSeen'

/** Re-open the modal with the full history from anywhere (the sidebar version). */
export const OPEN_WHATS_NEW = 'pg:open-whats-new'

const KIND_META: Record<
  ChangeKind,
  { label: string; badge: string; icon: typeof Sparkles }
> = {
  feature: { label: 'New', badge: 'bg-blue-50 text-blue-700 ring-blue-600/20', icon: Sparkles },
  improvement: {
    label: 'Improved',
    badge: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    icon: ArrowUpCircle,
  },
  fix: { label: 'Fixed', badge: 'bg-green-50 text-green-700 ring-green-600/20', icon: Bug },
}

function formatDate(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

interface WhatsNewModalProps {
  /** The running version, from GET /api/v1/version. Null until it loads. */
  version: string | null
}

/**
 * "What's new" modal: catch people up on what changed when a version ships.
 *
 * Auto-opens ONCE per version. It compares the running version against the last
 * one this browser acknowledged and lists only what is newer, so an operator who
 * has already seen 0.42.1 is not shown it again on every page load.
 *
 * Renders nothing when there is nothing newer, which is the common case.
 *
 * The version is a prop rather than a build-time constant on purpose: it comes
 * from the API, which reads the VERSION file. A copy baked into the frontend is
 * how frontend/src/version.ts ended up eleven releases stale.
 */
export function WhatsNewModal({ version }: WhatsNewModalProps) {
  const [open, setOpen] = useState(false)
  const [releases, setReleases] = useState<Release[]>([])

  useEffect(() => {
    // Wait for the real version; opening against a null one would either show
    // nothing or mark an unknown version as seen.
    if (!version) return

    let lastSeen: string | null = null
    try {
      lastSeen = localStorage.getItem(SEEN_KEY)
    } catch {
      // Private mode or storage disabled. Don't auto-open rather than
      // re-opening on every single page load.
      return
    }

    if (lastSeen !== version) {
      const fresh = releasesSince(lastSeen)
      if (fresh.length > 0) {
        setReleases(fresh)
        setOpen(true)
      }
    }
  }, [version])

  useEffect(() => {
    const onOpen = () => {
      // The full history as a reader would want it: releases with nothing in
      // them for a user are recorded in CHANGELOG.md, not shown here.
      setReleases(userFacingReleases())
      setOpen(true)
    }
    window.addEventListener(OPEN_WHATS_NEW, onOpen)
    return () => window.removeEventListener(OPEN_WHATS_NEW, onOpen)
  }, [])

  const handleClose = () => {
    setOpen(false)
    // Acknowledge the running version so it does not auto-open again.
    if (version) {
      try {
        localStorage.setItem(SEEN_KEY, version)
      } catch {
        // Nothing to do: it will offer itself again next time, which is a
        // better failure than crashing the app shell.
      }
    }
  }

  if (releases.length === 0) return null

  return (
    <Modal
      isOpen={open}
      onClose={handleClose}
      title="What's new"
      description={version ? `You're on v${version}.` : undefined}
      size="xl"
    >
      <ModalBody className="max-h-[60vh] overflow-y-auto">
        <div className="space-y-6">
          {releases.map((release) => (
            <section key={release.version}>
              <div className="flex items-baseline justify-between gap-3">
                <h3 className="text-sm font-semibold text-gray-900">{release.title}</h3>
                <span className="shrink-0 text-xs tabular-nums text-gray-500">
                  v{release.version} · {formatDate(release.date)}
                </span>
              </div>
              <ul className="mt-3 space-y-3">
                {release.highlights.map((h, i) => {
                  const meta = KIND_META[h.kind]
                  const Icon = meta.icon
                  return (
                    <li key={i} className="flex gap-2.5 text-sm">
                      <span
                        className={`mt-0.5 inline-flex h-5 shrink-0 items-center gap-1 rounded-full px-2 text-[10px] font-medium ring-1 ring-inset ${meta.badge}`}
                      >
                        <Icon className="h-3 w-3" />
                        {meta.label}
                      </span>
                      <span className="text-gray-600">{h.text}</span>
                    </li>
                  )
                })}
              </ul>
            </section>
          ))}
        </div>
      </ModalBody>
      <ModalFooter>
        <button
          onClick={handleClose}
          className="rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Got it
        </button>
      </ModalFooter>
    </Modal>
  )
}

export default WhatsNewModal

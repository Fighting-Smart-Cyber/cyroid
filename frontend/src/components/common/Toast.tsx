// frontend/src/components/common/Toast.tsx
/**
 * The application's one toast stack.
 *
 * There were two, at the same fixed coordinates: this one, mounted by the shell, and a second
 * one in components/notifications that the realtime notifications fed. Whichever rendered second
 * covered the first, so a range that failed to deploy and a password that failed to change could
 * each hide the other. That one is now a shim over this store (see
 * components/notifications/ToastContainer.tsx) and this is the only thing that draws a toast.
 *
 * Say something with `toast.success | error | warning | info | withActions` from
 * stores/toastStore. Mount this exactly once, in NotificationProvider -- it wraps every route,
 * including the pop-out consoles and the student lab, which the shell does not.
 */
import { useToastStore, ToastType, ToastAction } from '../../stores/toastStore'
import { X, CheckCircle, AlertCircle, AlertTriangle, Info } from 'lucide-react'
import clsx from 'clsx'

const iconMap: Record<ToastType, React.ReactNode> = {
  success: <CheckCircle className="w-5 h-5 text-green-400" />,
  error: <AlertCircle className="w-5 h-5 text-red-400" />,
  warning: <AlertTriangle className="w-5 h-5 text-yellow-400" />,
  info: <Info className="w-5 h-5 text-blue-400" />,
}

const bgColorMap: Record<ToastType, string> = {
  success: 'bg-green-900/90 border-green-700',
  error: 'bg-red-900/90 border-red-700',
  warning: 'bg-yellow-900/90 border-yellow-700',
  info: 'bg-blue-900/90 border-blue-700',
}

function ActionButton({
  action,
  toastId,
  removeToast,
}: {
  action: ToastAction
  toastId: string
  removeToast: (id: string) => void
}) {
  const handleClick = () => {
    action.onClick()
    removeToast(toastId)
  }

  return (
    <button
      onClick={handleClick}
      className={clsx(
        'px-3 py-1 text-xs font-medium rounded transition-colors',
        action.variant === 'primary'
          ? 'bg-white text-gray-900 hover:bg-gray-100'
          : 'bg-white/20 text-white hover:bg-white/30'
      )}
    >
      {action.label}
    </button>
  )
}

interface ToastContainerProps {
  /** How many are on screen at once. A deploy emits a step per workload and the rest of the
   *  screen should not disappear behind them; the bell keeps the full record either way. */
  maxVisible?: number
}

export function ToastContainer({ maxVisible = 3 }: ToastContainerProps = {}) {
  const toasts = useToastStore((state) => state.toasts)
  const removeToast = useToastStore((state) => state.removeToast)

  // The newest ones: the store appends, and the message that just arrived is the one being
  // waited on.
  const visible = toasts.slice(-maxVisible)

  // Rendered even when empty. A live region has to be in the document before something is put
  // into it, or a screen reader announces nothing; and the container takes no pointer events, so
  // an empty one cannot swallow a click in the corner it sits in.
  //
  // The width is clamped to the viewport as well as to 24rem: a toast at its full width on a
  // 375px screen started off the left edge and took the page's horizontal scrollbar with it.
  return (
    <div
      className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-[min(24rem,calc(100vw-2rem))] pointer-events-none"
      role="status"
      aria-live="polite"
      aria-label="Notifications"
    >
      {visible.map((toast) => (
        <div
          key={toast.id}
          className={`
            flex flex-col gap-2 px-4 py-3 rounded-lg border shadow-lg
            animate-slide-in-right pointer-events-auto
            ${bgColorMap[toast.type]}
          `}
        >
          <div className="flex items-start gap-3">
            <div className="flex-shrink-0 mt-0.5">
              {iconMap[toast.type]}
            </div>
            {/* min-w-0 and break-words together: a flex item will not shrink below its longest
                word on its own, and these messages carry range names, workload names and error
                strings that have no spaces in them. */}
            <p className="text-sm text-white flex-1 min-w-0 break-words">{toast.message}</p>
            <button
              onClick={() => removeToast(toast.id)}
              className="flex-shrink-0 text-gray-400 hover:text-white transition-colors"
              aria-label="Dismiss notification"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
          {toast.actions && toast.actions.length > 0 && (
            <div className="flex items-center gap-2 ml-8">
              {toast.actions.map((action, idx) => (
                <ActionButton
                  key={idx}
                  action={action}
                  toastId={toast.id}
                  removeToast={removeToast}
                />
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

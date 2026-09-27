// frontend/src/components/notifications/ToastContainer.tsx
/**
 * The realtime notifications' way into the one toast stack.
 *
 * This used to be a second, independent stack, with its own queue in React state and its own
 * fixed position -- the same bottom-right corner the shell's stack draws in. Both rendered on
 * every authenticated page, so a deploy notification and a page's own error message covered each
 * other and neither could be read or dismissed.
 *
 * The stack that survived is components/common/Toast.tsx over stores/toastStore, because that is
 * the one the rest of the application already talks to. This file keeps its old shape -- the
 * component and `useToasts` with the same signatures -- so NotificationProvider and anything else
 * holding it compiles unchanged, and forwards everything to that store.
 *
 * New code should not use `useToasts`. Call `toast.info(...)` from stores/toastStore.
 */
import { useCallback } from 'react'
import { ToastContainer as SharedToastStack } from '../common/Toast'
import { useToastStore } from '../../stores/toastStore'
import { NotificationSeverity } from '../../stores/notificationStore'

export interface ToastData {
  id: string
  message: string
  severity: NotificationSeverity
}

// A module constant, not a fresh literal per render: callers pass this into effect dependency
// arrays.
const NO_TOASTS: ToastData[] = []

interface ToastContainerProps {
  /** Ignored. The stack reads the store directly; kept so existing callers still compile. */
  toasts?: ToastData[]
  /** Ignored. Dismissal is the store's, so a toast raised anywhere can be dismissed here. */
  onDismiss?: (id: string) => void
  maxVisible?: number
}

/**
 * Mounts the one stack. This is where it lives for the whole application: NotificationProvider
 * wraps every route, including the pop-out consoles and the student lab, which the shell does
 * not, and toasts raised on those pages previously went nowhere.
 */
export function ToastContainer({ maxVisible = 3 }: ToastContainerProps) {
  return <SharedToastStack maxVisible={maxVisible} />
}

/**
 * Compatibility shim over the shared store.
 *
 * `toasts` is always empty: the container above reads the store itself, so there is nothing left
 * for a caller to render. Everything else does what it says, against the one queue.
 */
export function useToasts() {
  const addToast = useCallback((message: string, severity: NotificationSeverity) => {
    const store = useToastStore.getState()
    store.addToast({ type: severity, message })
    // The store assigns the id, so read it back rather than inventing a second one that would
    // not dismiss anything.
    const queue = useToastStore.getState().toasts
    return queue[queue.length - 1]?.id ?? ''
  }, [])

  const dismissToast = useCallback((id: string) => {
    useToastStore.getState().removeToast(id)
  }, [])

  const clearAllToasts = useCallback(() => {
    useToastStore.getState().clearToasts()
  }, [])

  return {
    toasts: NO_TOASTS,
    addToast,
    dismissToast,
    clearAllToasts,
  }
}

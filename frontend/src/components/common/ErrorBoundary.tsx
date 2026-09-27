// frontend/src/components/common/ErrorBoundary.tsx
/**
 * A boundary so one bad render does not take the whole application with it.
 *
 * React unmounts the entire tree when a render throws and nothing catches it. The user is left
 * on a white page with no message, no route and no way back except a browser reload -- during a
 * live exercise, losing whatever was on screen. Several panels render payload fields they do not
 * own (an inject's actions, a scenario's category, a FastAPI 422 body), so this is not a
 * hypothetical failure: each of those has blanked the app.
 *
 * Mount it around the routed content, and around any panel that renders a payload shape the
 * frontend does not define, so a broken panel costs the panel and not the page.
 *
 * `resetKey` is how a caught error is cleared: give it the current route path and navigating away
 * un-breaks the boundary. Without it the fallback is sticky and the user's only exit is a reload.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, RefreshCw, RotateCcw } from 'lucide-react'

interface Props {
  children: ReactNode
  /** Changing this clears a caught error. Pass the route path when wrapping routed content. */
  resetKey?: string | number
  /** What broke, in the user's words -- "the inject timeline". Defaults to "this page". */
  surface?: string
  /** Rendered instead of the default panel, for a boundary inside a small area. */
  fallback?: (error: Error, retry: () => void) => ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The fallback shows the message only; the stack is what a support conversation needs, and
    // the console is the one place it survives a reload-happy user.
    console.error('Unhandled render error', error, info.componentStack)
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  retry = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback) return this.props.fallback(error, this.retry)

    const surface = this.props.surface ?? 'this page'
    return (
      <div className="p-6">
        <div className="max-w-xl mx-auto bg-white border border-red-200 rounded-lg shadow-sm p-6">
          <div className="flex items-start gap-3">
            <AlertTriangle className="w-6 h-6 text-red-500 shrink-0" />
            <div className="min-w-0">
              <h2 className="text-lg font-medium text-gray-900">Something in {surface} failed to render</h2>
              <p className="text-sm text-gray-600 mt-1">
                The rest of the application is fine. Try again, or reload if it keeps happening.
              </p>
              <p className="mt-3 text-xs font-mono text-gray-500 break-words">
                {error.message || String(error)}
              </p>
              <p className="mt-1 text-xs text-gray-400 break-all">{window.location.pathname}</p>
              <div className="flex items-center gap-2 mt-4">
                <button
                  type="button"
                  onClick={this.retry}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-sm rounded-md bg-primary-600 text-white hover:bg-primary-700"
                >
                  <RotateCcw className="w-4 h-4" />
                  Try again
                </button>
                <button
                  type="button"
                  onClick={() => window.location.reload()}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-sm rounded-md border hover:bg-gray-50"
                >
                  <RefreshCw className="w-4 h-4" />
                  Reload
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary

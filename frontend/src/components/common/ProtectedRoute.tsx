// frontend/src/components/common/ProtectedRoute.tsx
import { ReactNode, useEffect, useRef } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { canAccessRoute, getLandingPage, getRoleLabel } from '../../utils/roleUtils'
import { pageNameFor } from '../layout/Layout'
import { toast } from '../../stores/toastStore'

interface ProtectedRouteProps {
  children: ReactNode
  requiredRoles?: string[]  // If specified, only these roles can access the route
}

/** "Administrators", "Administrators and Range Engineers", "A, B and C". */
function joinWithAnd(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? ''
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
}

/**
 * What to tell someone the shell has just turned away.
 *
 * The old message printed the route's own guard back at the user -- "requires one of: student" --
 * which names an internal identifier and not a reason. These are the words the perspective
 * switcher already uses, and where the account does hold the role it says which perspective to
 * come back in: switching to the student view and then losing a page is otherwise indisputable
 * evidence that the page is broken.
 *
 * Exported so it can be read on its own; it is pure.
 */
export function accessDeniedMessage(
  page: string | undefined,
  requiredRoles: string[],
  effectiveRole: string | null,
  heldRoles: string[]
): string {
  const audience = joinWithAnd(requiredRoles.map((role) => `${getRoleLabel(role)}s`))
  const subject = page ? `${page} is` : 'That page is'
  const refusal = audience ? `${subject} open to ${audience}.` : `${subject} not open to you.`

  const holdsIt = requiredRoles.some((role) => heldRoles.includes(role))
  if (holdsIt && effectiveRole) {
    return `${refusal} You are viewing as ${getRoleLabel(effectiveRole)}. Switch perspective to open it.`
  }
  return refusal
}

export default function ProtectedRoute({ children, requiredRoles }: ProtectedRouteProps) {
  const { user, token, isLoading, checkAuth, getEffectiveRole } = useAuthStore()
  const location = useLocation()

  useEffect(() => {
    if (token && !user) {
      checkAuth()
    }
  }, [token, user, checkAuth])

  const effectiveRole = user ? getEffectiveRole() : null
  const denied =
    !!user && !!requiredRoles && requiredRoles.length > 0 && !canAccessRoute(effectiveRole, requiredRoles)

  // In an effect, not in the render pass: saying it while rendering meant the refusal was raised
  // again on every re-render of a route the user was already being sent away from. The ref is
  // what keeps it to one: development remounts effects to prove they are repeatable, and two
  // copies of the same refusal stacked on each other is how this looked before.
  const refusalAnnouncedFor = useRef<string | null>(null)
  useEffect(() => {
    // Forgotten again as soon as a page does open, because this component outlives the route it
    // was mounted for: react-router renders every sibling route's element into the same position,
    // so one ProtectedRoute fiber -- and one ref -- serves the whole shell. Remembering the
    // refusal past the redirect turned the second attempt at the same page away in silence.
    if (!denied) {
      refusalAnnouncedFor.current = null
      return
    }
    if (refusalAnnouncedFor.current === location.pathname) return
    refusalAnnouncedFor.current = location.pathname
    toast.error(
      accessDeniedMessage(
        pageNameFor(location.pathname),
        requiredRoles ?? [],
        effectiveRole,
        user?.roles ?? []
      )
    )
    // Deliberately not the full dependency list: requiredRoles is an array literal in the route
    // table, so it is a new value on every render and listing it would re-raise the toast on
    // every render. `denied` already folds in the role and the roles it was checked against.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [denied, location.pathname])

  // Only while there is nobody to draw the page for. isLoading is the auth store's, and it is
  // also set by a password change from inside the app -- which replaced the whole shell, modal
  // included, with this spinner until the request came back.
  if (isLoading && !user) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-primary-600"></div>
      </div>
    )
  }

  if (!token) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  // Wait for user data before checking roles
  if (!user) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-primary-600"></div>
      </div>
    )
  }

  if (denied) {
    return <Navigate to={getLandingPage(effectiveRole)} replace />
  }

  return <>{children}</>
}

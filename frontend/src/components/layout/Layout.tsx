// frontend/src/components/layout/Layout.tsx
import { ReactNode, useState, useEffect, useMemo, useRef } from 'react'
import { Link, useNavigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { useCapabilitiesStore, type Feature } from '../../stores/capabilitiesStore'
import { versionApi, VersionInfo } from '../../services/api'
import WhatsNewModal, { OPEN_WHATS_NEW } from '../WhatsNewModal'
import { canAccessRoute } from '../../utils/roleUtils'
import {
  LayoutDashboard,
  Server,
  Network,
  LogOut,
  Menu,
  X,
  HardDrive,
  Key,
  LayoutTemplate,
  Target,
  Settings,
  BookOpen,
  CalendarDays,
  GraduationCap,
  Store,
  Sparkles,
  Inbox
} from 'lucide-react'
import clsx from 'clsx'
import PasswordChangeModal from '../common/PasswordChangeModal'
import { FeedbackButton } from '../feedback'
import { NotificationBell } from '../notifications'
import PerspectiveSwitcher from '../common/PerspectiveSwitcher'
import { BRANDING, pageTitle } from '../../lib/branding'

interface LayoutProps {
  children: ReactNode
}

interface NavItem {
  name: string
  href: string
  icon: typeof LayoutDashboard
  requiredRoles?: string[]  // If undefined, accessible by all roles
  // If set, the item is shown only where this install offers that feature. A Kubernetes install
  // has no image cache or Docker image library, and linking to them led to pages that could only
  // report failure.
  requiredFeature?: Feature
}

interface NavSection {
  title: string
  items: NavItem[]
  isStorefront?: boolean  // Special styling for marketplace/catalog section
}

// Standalone nav items (role-filtered)
const dashboardNav: NavItem = { name: 'Dashboard', href: '/', icon: LayoutDashboard, requiredRoles: ['admin', 'engineer', 'evaluator'] }
const studentPortalNav: NavItem = { name: 'Student Portal', href: '/student-portal', icon: GraduationCap, requiredRoles: ['student'] }
// The same page under the name staff would look for it by. Without an entry the learner's
// landing page had no link anywhere in the product, so the only way to see what a learner sees
// was to know the URL.
const learnerViewNav: NavItem = { name: 'Learner View', href: '/student-portal', icon: GraduationCap, requiredRoles: ['admin', 'engineer', 'evaluator'] }

/**
 * The sections, built on demand rather than held in a module constant.
 *
 * One of these titles carries the product name, and the branding endpoint answers after this
 * module is evaluated but before the first render: a constant froze the engine's default into
 * the sidebar, so a distribution that re-themed everything else still had the engine's name on
 * this one heading. Building the array inside the render pass reads the name that is current.
 */
function navSections(): NavSection[] {
  return [
    {
      title: 'Content Development',
      items: [
        { name: 'Content Library', href: '/content', icon: BookOpen, requiredRoles: ['admin', 'engineer', 'evaluator'] },
        // No Artifacts entry: /artifacts renders the words "Coming Soon" and nothing else, and a
        // sidebar link is a promise that the page exists. Put it back when there is a page.
      ]
    },
    {
      title: 'Range Development',
      items: [
        { name: 'Ranges', href: '/ranges', icon: Network, requiredRoles: ['admin', 'engineer', 'evaluator'] },
        { name: 'Range Blueprints', href: '/blueprints', icon: LayoutTemplate, requiredRoles: ['admin', 'engineer'] },
        { name: 'Training Scenarios', href: '/scenarios', icon: Target, requiredRoles: ['admin', 'engineer'] },
        { name: 'VM Library', href: '/vm-library', icon: Server, requiredRoles: ['admin', 'engineer'], requiredFeature: 'image_library' },
        { name: 'Image Cache', href: '/cache', icon: HardDrive, requiredRoles: ['admin', 'engineer'], requiredFeature: 'image_cache' },
      ]
    },
    {
      title: 'Event Management',
      items: [
        { name: 'Feedback', href: '/feedback', icon: Inbox, requiredRoles: ['admin', 'engineer'] },
        { name: 'Training Events', href: '/events', icon: CalendarDays },  // Accessible to all
      ]
    },
    {
      title: `${BRANDING.productName} Storefront`,
      isStorefront: true,  // Distinct button styling for marketplace
      items: [
        { name: 'Browse Catalog', href: '/catalog', icon: Store, requiredRoles: ['admin', 'engineer', 'evaluator'] },
      ]
    },
  ]
}

/** Pages the shell routes to but does not list in the sidebar. */
const UNLISTED_PAGES: Record<string, string> = {
  '/admin': 'Admin Settings',
  '/execution': 'Execution Console',
}

/**
 * The product's own name for the page at `pathname`, or undefined where the shell has none.
 *
 * Used for the browser tab and, by ProtectedRoute, to tell someone which page turned them away.
 * A detail route answers with its section -- /ranges/<id> is "Ranges" -- because the shell knows
 * the route and not the record; the pages rendered outside this shell set their own titles.
 */
export function pageNameFor(pathname: string): string | undefined {
  const candidates: Array<[string, string]> = [
    [dashboardNav.href, dashboardNav.name],
    [studentPortalNav.href, studentPortalNav.name],
    ...navSections().flatMap((section) =>
      section.items.map((item) => [item.href, item.name] as [string, string])
    ),
    ...Object.entries(UNLISTED_PAGES),
  ]

  let best: [string, string] | undefined
  for (const [href, name] of candidates) {
    // '/' prefixes everything, so it only ever answers for itself.
    if (href === '/') {
      if (pathname === '/') return name
      continue
    }
    if (pathname === href || pathname.startsWith(`${href}/`)) {
      if (!best || href.length > best[0].length) best = [href, name]
    }
  }
  return best?.[1]
}

export default function Layout({ children }: LayoutProps) {
  const { user, logout, passwordResetRequired, getEffectiveRole } = useAuthStore()
  const navigate = useNavigate()
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [showPasswordModal, setShowPasswordModal] = useState(false)
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null)
  const [versionUnavailable, setVersionUnavailable] = useState(false)
  const mobileNavRef = useRef<HTMLDivElement>(null)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const closeMenuButtonRef = useRef<HTMLButtonElement>(null)
  const sidebarWasOpen = useRef(false)

  useEffect(() => {
    versionApi.get()
      .then(res => setVersionInfo(res.data))
      .catch(() => setVersionUnavailable(true))
  }, [])

  // Get effective role for filtering navigation
  const effectiveRole = getEffectiveRole()
  const features = useCapabilitiesStore((s) => s.features)

  // Filter sections - only show sections that have at least one visible item
  const filteredSections = useMemo(() => {
    return navSections().map(section => ({
      ...section,
      items: section.items.filter(
        item =>
          canAccessRoute(effectiveRole, item.requiredRoles) &&
          (!item.requiredFeature || (features?.[item.requiredFeature] ?? false))
      )
    })).filter(section => section.items.length > 0)
  }, [effectiveRole, features])

  // Check if standalone nav items are visible
  const showDashboard = canAccessRoute(effectiveRole, dashboardNav.requiredRoles)
  // One destination, two names. A learner sees their portal; staff see the same page labelled
  // as what it is to them -- the learner's view of their own assignments.
  const isLearnersOwnPortal = canAccessRoute(effectiveRole, studentPortalNav.requiredRoles)
  const portalNav = isLearnersOwnPortal ? studentPortalNav : learnerViewNav
  const showStudentPortal =
    isLearnersOwnPortal || canAccessRoute(effectiveRole, learnerViewNav.requiredRoles)

  // The perspective, not the account. An admin who switches to the student perspective is asking
  // to see what a student sees, and the admin route itself already refuses them while they are
  // there -- so keeping the link offered a page that would turn them away.
  const showAdminSettings = effectiveRole === 'admin'

  // The tab names the page and the product together, from the one branding helper. index.html
  // carries a title too, for the moment before this mounts; leaving it as the answer is how an
  // install ended up with one name in the tab and another in the sidebar.
  useEffect(() => {
    document.title = pageTitle(pageNameFor(location.pathname))
  }, [location.pathname])

  // Back to the bare product name when the shell goes away, so signing out of Ranges does not
  // leave a login screen whose tab still claims to be showing ranges. The pages that render
  // outside this shell -- the pop-out consoles, the student lab -- set their own titles after
  // this runs, so they are unaffected.
  useEffect(() => () => { document.title = pageTitle() }, [])

  // The mobile sidebar is moved off-screen with a transform, which hides it from the eye and not
  // from the keyboard: tabbing from the menu button walked the whole closed menu before reaching
  // the page. `inert` takes it out of the tab order and the accessibility tree while leaving the
  // element in place, so the slide transition survives.
  useEffect(() => {
    const el = mobileNavRef.current
    if (el) {
      if (sidebarOpen) el.removeAttribute('inert')
      else el.setAttribute('inert', '')
    }
    // Follow the menu: into it when it opens, back to the button that opened it when it closes.
    if (sidebarOpen) closeMenuButtonRef.current?.focus()
    else if (sidebarWasOpen.current) menuButtonRef.current?.focus()
    sidebarWasOpen.current = sidebarOpen
  }, [sidebarOpen])

  useEffect(() => {
    if (!sidebarOpen) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSidebarOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [sidebarOpen])

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  // Show forced password modal if required
  const shouldShowForcedPasswordModal = passwordResetRequired && !showPasswordModal

  const versionLabel = versionInfo
    ? `v${versionInfo.version}${versionInfo.commit !== 'dev' ? ` (${versionInfo.commit.substring(0, 7)})` : ''}`
    : versionUnavailable
      ? 'Version unknown'
      : 'Checking version...'

  return (
    <div className="min-h-screen bg-gray-50">
      {/* First thing the keyboard reaches: the sidebar is long, and without this every page
          started with a walk through it. */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[60] focus:rounded-md focus:bg-white focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-gray-900 focus:shadow-lg focus:ring-2 focus:ring-primary-500"
      >
        Skip to main content
      </a>

      {/* Mobile sidebar backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-gray-600 bg-opacity-75 lg:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Mobile sidebar */}
      <div
        ref={mobileNavRef}
        id="mobile-navigation"
        className={clsx(
          "fixed inset-y-0 left-0 z-50 w-64 bg-gray-900 transform transition-transform duration-300 lg:hidden",
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        <div className="flex items-center justify-between h-16 px-4 bg-gray-800">
          <span className="text-xl font-bold text-white">{BRANDING.productName}</span>
          <button
            ref={closeMenuButtonRef}
            onClick={() => setSidebarOpen(false)}
            className="text-gray-300 hover:text-white"
            aria-label="Close navigation"
          >
            <X className="h-6 w-6" />
          </button>
        </div>
        <nav className="mt-4 px-2 space-y-1" aria-label="Primary">
          {/* Dashboard or Student Portal - standalone */}
          {showDashboard && (
            <Link
              to={dashboardNav.href}
              onClick={() => setSidebarOpen(false)}
              className={clsx(
                "flex items-center px-3 py-2 text-sm font-medium rounded-md",
                location.pathname === dashboardNav.href
                  ? "bg-gray-800 text-white"
                  : "text-gray-300 hover:bg-gray-700 hover:text-white"
              )}
            >
              <dashboardNav.icon className="mr-3 h-5 w-5" />
              {dashboardNav.name}
            </Link>
          )}
          {showStudentPortal && (
            <Link
              to={portalNav.href}
              onClick={() => setSidebarOpen(false)}
              className={clsx(
                "flex items-center px-3 py-2 text-sm font-medium rounded-md",
                location.pathname === portalNav.href
                  ? "bg-gray-800 text-white"
                  : "text-gray-300 hover:bg-gray-700 hover:text-white"
              )}
            >
              <portalNav.icon className="mr-3 h-5 w-5" />
              {portalNav.name}
            </Link>
          )}

          {/* Sectioned navigation */}
          {filteredSections.map((section, index) => (
            <div key={section.title} className="pt-4">
              {index > 0 && <div className="mx-3 mb-3 border-t border-gray-700" />}
              <h3 className="px-3 text-xs font-semibold text-gray-400 uppercase tracking-wider">
                {section.title}
              </h3>
              <div className="mt-2 space-y-1">
                {section.items.map((item) => (
                  <Link
                    key={item.name}
                    to={item.href}
                    onClick={() => setSidebarOpen(false)}
                    className={clsx(
                      "flex items-center px-3 py-2 text-sm font-medium rounded-md",
                      section.isStorefront
                        ? location.pathname === item.href || location.pathname.startsWith(item.href + '/')
                          ? "bg-gradient-to-r from-indigo-600 to-purple-600 text-white shadow-lg shadow-indigo-500/25"
                          : "bg-gradient-to-r from-indigo-600/80 to-purple-600/80 text-white hover:from-indigo-500 hover:to-purple-500"
                        : location.pathname === item.href
                          ? "bg-gray-800 text-white"
                          : "text-gray-300 hover:bg-gray-700 hover:text-white"
                    )}
                  >
                    <item.icon className="mr-3 h-5 w-5" />
                    {item.name}
                    {section.isStorefront && (
                      <Sparkles className="ml-auto h-4 w-4 text-yellow-300" />
                    )}
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </nav>
        {/* Mobile user section */}
        <div className="absolute bottom-0 left-0 right-0 px-2 pb-4 bg-gray-900">
          <div className="px-3 py-2 text-sm text-gray-400 border-t border-gray-700 pt-4">
            <div>Signed in as <span className="font-medium text-white">{user?.username}</span></div>
            {/* Role Perspective Switcher (Mobile) */}
            {user?.roles && user.roles.length > 0 && (
              <div className="mt-2">
                <PerspectiveSwitcher />
              </div>
            )}
          </div>
          {showAdminSettings && (
            <Link
              to="/admin"
              onClick={() => setSidebarOpen(false)}
              className={clsx(
                "flex items-center w-full px-3 py-2 text-sm font-medium rounded-md",
                location.pathname === '/admin'
                  ? "bg-gray-800 text-white"
                  : "text-gray-300 hover:bg-gray-700 hover:text-white"
              )}
            >
              <Settings className="mr-3 h-5 w-5" />
              Admin Settings
            </Link>
          )}
          <button
            onClick={() => { setShowPasswordModal(true); setSidebarOpen(false); }}
            className="flex items-center w-full px-3 py-2 text-sm font-medium text-gray-300 rounded-md hover:bg-gray-700 hover:text-white"
          >
            <Key className="mr-3 h-5 w-5" />
            Change Password
          </button>
          <button
            onClick={handleLogout}
            className="flex items-center w-full px-3 py-2 text-sm font-medium text-gray-300 rounded-md hover:bg-gray-700 hover:text-white"
          >
            <LogOut className="mr-3 h-5 w-5" />
            Sign out
          </button>
        </div>
      </div>

      {/* Desktop sidebar - z-30 ensures notification dropdown overlays main content */}
      <div className="hidden lg:fixed lg:inset-y-0 lg:flex lg:w-64 lg:flex-col lg:z-30">
        {/* Header outside scrollable area so dropdown isn't clipped */}
        <div className="flex items-center justify-between h-16 px-4 bg-gray-800 relative z-20">
          <span className="text-xl font-bold text-white">{BRANDING.productName}</span>
          <div className="flex items-center gap-1">
            <FeedbackButton />
            <NotificationBell />
          </div>
        </div>
        <div className="flex flex-col flex-grow bg-gray-900 overflow-y-auto">
          <nav className="mt-4 flex-1 px-2 space-y-1" aria-label="Primary">
            {/* Dashboard or Student Portal - standalone */}
            {showDashboard && (
              <Link
                to={dashboardNav.href}
                className={clsx(
                  "flex items-center px-3 py-2 text-sm font-medium rounded-md",
                  location.pathname === dashboardNav.href
                    ? "bg-gray-800 text-white"
                    : "text-gray-300 hover:bg-gray-700 hover:text-white"
                )}
              >
                <dashboardNav.icon className="mr-3 h-5 w-5" />
                {dashboardNav.name}
              </Link>
            )}
            {showStudentPortal && (
              <Link
                to={portalNav.href}
                className={clsx(
                  "flex items-center px-3 py-2 text-sm font-medium rounded-md",
                  location.pathname === portalNav.href
                    ? "bg-gray-800 text-white"
                    : "text-gray-300 hover:bg-gray-700 hover:text-white"
                )}
              >
                <portalNav.icon className="mr-3 h-5 w-5" />
                {portalNav.name}
              </Link>
            )}

            {/* Sectioned navigation */}
            {filteredSections.map((section, index) => (
              <div key={section.title} className="pt-4">
                {index > 0 && <div className="mx-3 mb-3 border-t border-gray-700" />}
                <h3 className="px-3 text-xs font-semibold text-gray-400 uppercase tracking-wider">
                  {section.title}
                </h3>
                <div className="mt-2 space-y-1">
                  {section.items.map((item) => (
                    <Link
                      key={item.name}
                      to={item.href}
                      className={clsx(
                        "flex items-center px-3 py-2 text-sm font-medium rounded-md",
                        section.isStorefront
                          ? location.pathname === item.href || location.pathname.startsWith(item.href + '/')
                            ? "bg-gradient-to-r from-indigo-600 to-purple-600 text-white shadow-lg shadow-indigo-500/25"
                            : "bg-gradient-to-r from-indigo-600/80 to-purple-600/80 text-white hover:from-indigo-500 hover:to-purple-500"
                          : location.pathname === item.href
                            ? "bg-gray-800 text-white"
                            : "text-gray-300 hover:bg-gray-700 hover:text-white"
                      )}
                    >
                      <item.icon className="mr-3 h-5 w-5" />
                      {item.name}
                      {section.isStorefront && (
                        <Sparkles className="ml-auto h-4 w-4 text-yellow-300" />
                      )}
                    </Link>
                  ))}
                </div>
              </div>
            ))}
          </nav>
          <div className="px-2 pb-4">
            <div className="px-3 py-2 text-sm text-gray-400">
              <div>Signed in as <span className="font-medium text-white">{user?.username}</span></div>
              {/* Role Perspective Switcher */}
              {user?.roles && user.roles.length > 0 && (
                <div className="mt-2">
                  <PerspectiveSwitcher />
                </div>
              )}
              {user?.tags && user.tags.length > 0 && (
                <div className="mt-2 flex items-center flex-wrap gap-1">
                  {user.tags.slice(0, 3).map((tag) => (
                    <span key={tag} className="text-xs bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">{tag}</span>
                  ))}
                  {user.tags.length > 3 && <span className="text-xs text-gray-500">+{user.tags.length - 3}</span>}
                </div>
              )}
            </div>
            {showAdminSettings && (
              <Link
                to="/admin"
                className={clsx(
                  "flex items-center w-full px-3 py-2 text-sm font-medium rounded-md",
                  location.pathname === '/admin'
                    ? "bg-gray-800 text-white"
                    : "text-gray-300 hover:bg-gray-700 hover:text-white"
                )}
              >
                <Settings className="mr-3 h-5 w-5" />
                Admin Settings
              </Link>
            )}
            <button
              onClick={() => setShowPasswordModal(true)}
              className="flex items-center w-full px-3 py-2 text-sm font-medium text-gray-300 rounded-md hover:bg-gray-700 hover:text-white"
            >
              <Key className="mr-3 h-5 w-5" />
              Change Password
            </button>
            <button
              onClick={handleLogout}
              className="flex items-center w-full px-3 py-2 text-sm font-medium text-gray-300 rounded-md hover:bg-gray-700 hover:text-white"
            >
              <LogOut className="mr-3 h-5 w-5" />
              Sign out
            </button>
            {/* The release notes are reachable from here and nowhere else, so this block stays
                rendered when /version does not answer: hiding it took the changelog with it,
                and an operator cannot tell a version that failed to load from one that was
                never there. */}
            <div className="mt-4 pt-4 border-t border-gray-700 px-3 text-xs text-gray-500">
              <div>{versionLabel}</div>
              <button
                onClick={() => window.dispatchEvent(new Event(OPEN_WHATS_NEW))}
                className="mt-1 hover:text-gray-300 hover:underline"
              >
                Release notes
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Main content */}
      <div className="lg:pl-64 flex flex-col flex-1">
        {/* Top bar */}
        <div className="sticky top-0 z-10 flex h-16 bg-white shadow lg:hidden">
          <button
            ref={menuButtonRef}
            onClick={() => setSidebarOpen(true)}
            className="px-4 text-gray-500 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-primary-500"
            aria-label="Open navigation"
            aria-expanded={sidebarOpen}
            aria-controls="mobile-navigation"
          >
            <Menu className="h-6 w-6" />
          </button>
          <div className="flex items-center justify-between flex-1 px-4">
            <span className="text-lg font-semibold text-gray-900">{BRANDING.productName}</span>
            <div className="flex items-center gap-1">
              <FeedbackButton className="text-gray-500 hover:text-gray-900 hover:bg-gray-100" />
              <NotificationBell />
            </div>
          </div>
        </div>

        {/* Page content. tabIndex so the skip link can hand focus to it. */}
        <main id="main-content" tabIndex={-1} className="flex-1 focus:outline-none">
          <div className="py-6 px-4 sm:px-6 lg:px-8">
            {children}
          </div>
        </main>
      </div>

      {/* Voluntary password change modal */}
      <PasswordChangeModal
        isOpen={showPasswordModal}
        onClose={() => setShowPasswordModal(false)}
        isForced={false}
      />

      {/* Forced password change modal (when admin requires reset) */}
      <PasswordChangeModal
        isOpen={shouldShowForcedPasswordModal}
        onClose={() => {}}
        isForced={true}
      />

      {/* Release notes. Auto-opens once per version, and renders nothing when
          this browser has already acknowledged the running one. */}
      <WhatsNewModal version={versionInfo?.version ?? null} />

      {/* Toasts are NOT mounted here. There is one stack for the whole application and
          NotificationProvider mounts it, so it also reaches the pages that render outside this
          shell -- the pop-out consoles and the student lab. Mounting a second one here is what
          left two stacks of toasts covering each other at the same coordinates. */}
    </div>
  )
}

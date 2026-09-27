// frontend/src/App.tsx
import { Routes, Route, Navigate, Link, useLocation } from 'react-router-dom'
import { ReactNode, useEffect } from 'react'
import { useAuthStore } from './stores/authStore'
import { useSystemStore } from './stores/systemStore'
import { useCapabilitiesStore } from './stores/capabilitiesStore'
import type { Feature } from './stores/capabilitiesStore'
import { featureGate } from './lib/featureGate'
import { toast } from './stores/toastStore'
import { NotificationProvider } from './providers/NotificationProvider'
import Login from './pages/Login'
import Register from './pages/Register'
import Dashboard from './pages/Dashboard'
import FeedbackPage from './pages/Feedback'
import VMLibrary from './pages/VMLibrary'
import Ranges from './pages/Ranges'
import RangeDetail from './pages/RangeDetail'
import RangeWizardPage from './pages/RangeWizardPage'
import ExecutionConsole from './pages/ExecutionConsole'
import StandaloneConsole from './pages/StandaloneConsole'
import KubeVirtStandaloneConsole from './pages/KubeVirtStandaloneConsole'
import Blueprints from './pages/Blueprints'
import BlueprintDetail from './pages/BlueprintDetail'
import TrainingScenarios from './pages/TrainingScenarios'
import StudentLab from './pages/StudentLab'
import StudentPortal from './pages/StudentPortal'
import ImageCache from './pages/ImageCache'
import Admin from './pages/Admin'
import ContentLibrary from './pages/ContentLibrary'
import ContentEditor from './pages/ContentEditor'
import TrainingEvents from './pages/TrainingEvents'
import TrainingEventDetail from './pages/TrainingEventDetail'
import CatalogBrowser from './pages/CatalogBrowser'
import CatalogItemDetail from './pages/CatalogItemDetail'
import ProtectedRoute from './components/common/ProtectedRoute'
import ErrorBoundary from './components/common/ErrorBoundary'
import Layout from './components/layout/Layout'

interface FeatureRouteProps {
  feature: Feature
  /** Named in the refusal, so the user is told which page turned them away. */
  surface: string
  /** Where the same job gets done on the substrate this install actually runs. */
  instead: string
  children: ReactNode
}

/**
 * A route that only one substrate offers.
 *
 * Gating the nav link hid these pages; it did not make them unreachable, and a bookmarked /cache
 * on a Kubernetes install still rendered a page whose every request fails against a daemon that
 * is not there. A refusal naming the substrate is a worse page than the one the user wanted and a
 * far better one than a screen of failed panels.
 *
 * Inside ProtectedRoute, never outside it: role is the first question, and the dashboard this
 * redirects to is not somewhere every role may land.
 */
function FeatureRoute({ feature, surface, instead, children }: FeatureRouteProps) {
  const features = useCapabilitiesStore((state) => state.features)
  const substrateLabel = useCapabilitiesStore((state) => state.substrateLabel)
  const gate = featureGate(features, feature)

  useEffect(() => {
    if (gate === 'deny') {
      toast.error(
        `${surface} is not available on ${substrateLabel ?? 'this substrate'}. ${instead}`
      )
    }
  }, [gate, surface, instead, substrateLabel])

  if (gate === 'pending') return null
  if (gate === 'deny') return <Navigate to="/" replace />
  return <>{children}</>
}

function NotFound() {
  return (
    <div className="text-center py-16">
      <h1 className="text-2xl font-semibold text-gray-900">Page not found</h1>
      <p className="mt-2 text-sm text-gray-500">
        Nothing is served at this address on this install.
      </p>
      <Link to="/" className="mt-6 inline-block text-sm text-blue-600 hover:underline">
        Go to the dashboard
      </Link>
    </div>
  )
}

function App() {
  const location = useLocation()
  const { checkAuth, token } = useAuthStore()
  const fetchSystemInfo = useSystemStore((state) => state.fetchSystemInfo)
  const fetchCapabilities = useCapabilitiesStore((state) => state.fetchCapabilities)

  useEffect(() => {
    if (token) {
      checkAuth()
    }
  }, [])

  useEffect(() => {
    fetchSystemInfo()
    fetchCapabilities()
  }, [fetchSystemInfo, fetchCapabilities])

  return (
    <NotificationProvider>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
      {/* Standalone console - protected but no layout (for pop-out windows) */}
      <Route
        path="/console/k8s/:rangeId/:workload"
        element={
          <ProtectedRoute>
            <KubeVirtStandaloneConsole />
          </ProtectedRoute>
        }
      />
      <Route
        path="/console/:vmId"
        element={
          <ProtectedRoute>
            <StandaloneConsole />
          </ProtectedRoute>
        }
      />
      {/* Student Lab - protected but no layout (immersive experience) */}
      <Route
        path="/lab/:rangeId"
        element={
          <ProtectedRoute>
            <StudentLab />
          </ProtectedRoute>
        }
      />
      <Route
        path="/*"
        element={
          <ProtectedRoute>
            <Layout>
              {/* One boundary around the routed content, keyed on the path: a panel that
                  renders a payload shape the frontend does not own used to take the whole
                  application to a white page, and navigating away is how the user recovers. */}
              <ErrorBoundary resetKey={location.pathname}>
              <Routes>
                {/* Dashboard - accessible to admin, engineer, evaluator */}
                <Route path="/" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <Dashboard />
                  </ProtectedRoute>
                } />

                {/* The learner's landing page. Staff roles are admitted too: every request
                    it makes is scoped to the caller server-side (/ranges/my-ranges and
                    /training-events/my-events both filter by the session), so an instructor
                    opening it sees their own assignments, not a learner's. Refusing them made
                    the page unverifiable -- on a fresh install the only account is an admin,
                    and they were told "this page requires one of: student". */}
                <Route path="/student-portal" element={
                  <ProtectedRoute requiredRoles={['student', 'admin', 'engineer', 'evaluator']}>
                    <StudentPortal />
                  </ProtectedRoute>
                } />

                {/* Range Engineering - admin and engineer only */}
                <Route path="/vm-library" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer']}>
                    <FeatureRoute
                      feature="image_library"
                      surface="The VM library"
                      instead="A range's machines are declared in its blueprint."
                    >
                      <VMLibrary />
                    </FeatureRoute>
                  </ProtectedRoute>
                } />
                <Route path="/cache" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer']}>
                    <FeatureRoute
                      feature="image_cache"
                      surface="The image cache"
                      instead="Range images are pulled by the cluster from the blueprint that names them."
                    >
                      <ImageCache />
                    </FeatureRoute>
                  </ProtectedRoute>
                } />
                <Route path="/blueprints" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer']}>
                    <Blueprints />
                  </ProtectedRoute>
                } />

                {/* Reading what customers sent. `admin, engineer` is the server's own
                    STAFF_ROLES -- the API scopes rows to the author for anyone else, so a
                    narrower gate here would only hide a page that would have worked. */}
                <Route path="/feedback" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer']}>
                    <FeedbackPage />
                  </ProtectedRoute>
                } />
                <Route path="/blueprints/:id" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer']}>
                    <BlueprintDetail />
                  </ProtectedRoute>
                } />
                <Route path="/scenarios" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer']}>
                    <TrainingScenarios />
                  </ProtectedRoute>
                } />
                <Route path="/ranges/new" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer']}>
                    <FeatureRoute
                      feature="range_composition"
                      surface="The range wizard"
                      instead="Deploy a range from a blueprint instead."
                    >
                      <RangeWizardPage />
                    </FeatureRoute>
                  </ProtectedRoute>
                } />
                {/* Ranges - accessible to admin, engineer, evaluator */}
                <Route path="/ranges" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <Ranges />
                  </ProtectedRoute>
                } />
                <Route path="/ranges/:id" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <RangeDetail />
                  </ProtectedRoute>
                } />
                <Route path="/execution/:rangeId" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <ExecutionConsole />
                  </ProtectedRoute>
                } />

                {/* Content - accessible to admin, engineer, evaluator */}
                <Route path="/content" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <ContentLibrary />
                  </ProtectedRoute>
                } />
                <Route path="/content/:id" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <ContentEditor />
                  </ProtectedRoute>
                } />

                {/* Catalog - accessible to admin, engineer, evaluator */}
                <Route path="/catalog" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <CatalogBrowser />
                  </ProtectedRoute>
                } />
                <Route path="/catalog/:sourceId/:itemId" element={
                  <ProtectedRoute requiredRoles={['admin', 'engineer', 'evaluator']}>
                    <CatalogItemDetail />
                  </ProtectedRoute>
                } />

                {/* Training Events - accessible to all authenticated users */}
                <Route path="/events" element={<TrainingEvents />} />
                <Route path="/events/:id" element={<TrainingEventDetail />} />

                {/* Admin only. User administration is the Admin page's Users tab and nothing
                    else: /users was a second copy of the same screen that no link in the shell
                    ever reached, so the two drifted with no way to tell which one an operator
                    had been working in. */}
                <Route path="/admin" element={
                  <ProtectedRoute requiredRoles={['admin']}>
                    <Admin />
                  </ProtectedRoute>
                } />

                {/* An unknown URL rendered the shell around nothing at all, which reads as a
                    page that failed to load rather than one that does not exist. */}
                <Route path="*" element={<NotFound />} />
              </Routes>
              </ErrorBoundary>
            </Layout>
          </ProtectedRoute>
        }
      />
      </Routes>
    </NotificationProvider>
  )
}

export default App

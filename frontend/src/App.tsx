import { useMemo } from 'react'
import { BrowserRouter, Navigate, useRoutes } from 'react-router-dom'
import type { RouteObject } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useAuth, useCanFn } from './core/auth'
import type { NavItem } from './core/registry'
import { readPref } from './core/storage'
import { DISMISSED } from './features/start/StartPage'
import { useModules } from './core/modules'
import { RealtimeProvider } from './core/RealtimeProvider'
import { features } from './features/enabled'
import { LoginPage } from './features/auth/LoginPage'
import { SetupPage } from './features/auth/SetupPage'
import { Layout } from './shell/Layout'

// Where to land: the checklist for an administrator who has not hidden it yet, else the first
// page of the navigation.
function Landing({ nav }: { nav: NavItem[] }) {
  const can = useCanFn()
  const showStart = can('users.create') && readPref(DISMISSED) !== '1' && nav.some((n) => n.id === 'start')
  // The first page this account is allowed to open, so nobody lands on a page that refuses them.
  const first = nav.find((n) => n.id !== 'start' && (!n.requiresRight || can(n.requiresRight)))
  return <Navigate to={showStart ? '/start' : (first?.to ?? '/nodes')} replace />
}

// Features whose backend module is not enabled are left out entirely.
function AuthenticatedApp() {
  const { t } = useTranslation()
  const modules = useModules()
  const enabled = useMemo(
    () => (modules ? features.filter((f) => !f.requiresModule || modules.has(f.requiresModule)) : []),
    [modules],
  )
  const nav = useMemo(() => enabled.flatMap((f) => f.nav ?? []), [enabled])
  const routes = useMemo<RouteObject[]>(
    () => [
      {
        element: <Layout nav={nav} />,
        children: [
          ...enabled.flatMap((f) => f.routes ?? []),
          { index: true, element: <Landing nav={nav} /> },
          { path: '*', element: <p>{t('notFound')}</p> },
        ],
      },
    ],
    [enabled, nav, t],
  )
  const element = useRoutes(routes)
  return modules ? element : <p className="loading">{t('loading')}</p>
}

function Gate() {
  const { t } = useTranslation()
  const { state } = useAuth()
  if (state.status === 'loading') return <p className="loading">{t('loading')}</p>
  if (state.status === 'setup') return <SetupPage />
  if (state.status === 'anonymous') return <LoginPage />
  return (
    <RealtimeProvider>
      <AuthenticatedApp />
    </RealtimeProvider>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Gate />
    </BrowserRouter>
  )
}

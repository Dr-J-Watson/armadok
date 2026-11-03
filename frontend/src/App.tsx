import { useMemo } from 'react'
import { BrowserRouter, Navigate, useRoutes } from 'react-router-dom'
import type { RouteObject } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useAuth } from './core/auth'
import { useModules } from './core/modules'
import { features } from './features/enabled'
import { LoginPage } from './features/auth/LoginPage'
import { SetupPage } from './features/auth/SetupPage'
import { Layout } from './shell/Layout'

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
          { index: true, element: <Navigate to={nav[0]?.to ?? '/nodes'} replace /> },
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
  return <AuthenticatedApp />
}

export default function App() {
  return (
    <BrowserRouter>
      <Gate />
    </BrowserRouter>
  )
}

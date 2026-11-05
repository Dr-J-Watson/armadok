import type { Feature } from '../../core/registry'
import { DashboardPage } from './DashboardPage'

export const dashboardFeature: Feature = {
  name: 'dashboard',
  nav: [{ id: 'dashboard', icon: 'dashboard', section: 'main', labelKey: 'nav.dashboard', to: '/dashboard' }],
  routes: [{ path: '/dashboard', element: <DashboardPage /> }],
}

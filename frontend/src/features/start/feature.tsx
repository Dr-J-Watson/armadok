import type { Feature } from '../../core/registry'
import { StartPage } from './StartPage'

export const startFeature: Feature = {
  name: 'start',
  requiresModule: 'diagnostics',
  nav: [{ id: 'start', icon: 'rocket', section: 'main', labelKey: 'nav.start', to: '/start', requiresRight: 'users.create' }],
  routes: [{ path: '/start', element: <StartPage /> }],
}

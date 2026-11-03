import type { Feature } from '../../core/registry'
import { AccountPage } from './AccountPage'

export const accountFeature: Feature = {
  name: 'account',
  requiresModule: 'users',
  nav: [{ id: 'account', labelKey: 'nav.account', to: '/account' }],
  routes: [{ path: '/account', element: <AccountPage /> }],
}

import type { Feature } from '../../core/registry'
import { AccountPage } from './AccountPage'

export const accountFeature: Feature = {
  name: 'account',
  requiresModule: 'users',
  routes: [{ path: '/account', element: <AccountPage /> }],
}

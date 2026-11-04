import type { Feature } from '../../core/registry'
import { ServiceAccountsPage } from './ServiceAccountsPage'

export const serviceAccountsFeature: Feature = {
  name: 'service_accounts',
  requiresModule: 'service_accounts',
  signals: { 'service-accounts.changed': ['service-accounts', 'service-account-keys', 'users'] },
  nav: [
    {
      id: 'service-accounts', section: 'access',
      labelKey: 'nav.serviceAccounts',
      to: '/service-accounts',
      requiresRight: 'service_accounts.manage',
    },
  ],
  routes: [{ path: '/service-accounts', element: <ServiceAccountsPage /> }],
}

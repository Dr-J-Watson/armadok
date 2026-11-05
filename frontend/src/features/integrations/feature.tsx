import type { Feature } from '../../core/registry'
import { IntegrationsPage } from './IntegrationsPage'

export const integrationsFeature: Feature = {
  name: 'integrations',
  requiresModule: 'hooks',
  signals: { 'hooks.changed': ['hooks'] },
  nav: [{ id: 'integrations', icon: 'plug', section: 'access', labelKey: 'nav.integrations', to: '/integrations', requiresRight: 'hooks.manage' }],
  routes: [{ path: '/integrations', element: <IntegrationsPage /> }],
}

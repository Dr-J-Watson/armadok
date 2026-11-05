import type { Feature } from '../../core/registry'
import { VulnsPage } from './VulnsPage'

export const vulnsFeature: Feature = {
  name: 'vulns',
  requiresModule: 'vulns',
  signals: { 'vulns.changed': ['vulns'] },
  nav: [{ id: 'vulns', icon: 'bug', section: 'admin', labelKey: 'nav.vulns', to: '/vulns', requiresRight: 'vulns.read' }],
  routes: [{ path: '/vulns', element: <VulnsPage /> }],
}

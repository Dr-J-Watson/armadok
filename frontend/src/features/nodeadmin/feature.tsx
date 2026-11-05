import type { Feature } from '../../core/registry'
import { NodeAdminPage } from './NodeAdminPage'

export const nodeAdminFeature: Feature = {
  name: 'nodeadmin',
  requiresModule: 'nodeadmin',
  signals: { 'admin.changed': ['admin-nodes', 'nodes'] },
  nav: [{ id: 'node-admin', icon: 'shield', section: 'admin', labelKey: 'nav.nodeAdmin', to: '/admin/nodes', requiresRight: 'nodes.admin' }],
  routes: [{ path: '/admin/nodes', element: <NodeAdminPage /> }],
}

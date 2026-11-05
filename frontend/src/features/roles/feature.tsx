import type { Feature } from '../../core/registry'
import { AccessPage } from './AccessPage'
import { NodeAccessTab } from './NodeAccessTab'

export const rolesFeature: Feature = {
  name: 'roles',
  requiresModule: 'roles',
  signals: {
    'roles.changed': ['roles', 'effective', 'node-access'],
    'groups.changed': ['groups', 'effective', 'node-access'],
    'assignments.changed': ['assignments', 'effective', 'node-access'],
  },
  nav: [{ id: 'access', icon: 'key', section: 'access', labelKey: 'nav.access', to: '/access', requiresRight: 'users.read' }],
  routes: [{ path: '/access', element: <AccessPage /> }],
  nodeTabs: [
    {
      id: 'access',
      labelKey: 'roles:tab',
      requiresRight: 'grants.manage',
      render: ({ nodeId }) => <NodeAccessTab nodeId={nodeId} />,
    },
  ],
}

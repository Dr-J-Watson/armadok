import type { Feature } from '../../core/registry'
import { AuditList } from './AuditList'
import { AuditPage } from './AuditPage'

export const auditFeature: Feature = {
  name: 'audit',
  signals: { 'audit.changed': ['audit'] },
  requiresModule: 'audit',
  nav: [{ id: 'audit', section: 'admin', labelKey: 'nav.audit', to: '/audit', requiresRight: 'audit.read' }],
  routes: [{ path: '/audit', element: <AuditPage /> }],
  nodeTabs: [
    {
      id: 'activity',
      labelKey: 'audit:tab',
      render: ({ nodeId }) => <AuditList nodeId={nodeId} />,
    },
  ],
}

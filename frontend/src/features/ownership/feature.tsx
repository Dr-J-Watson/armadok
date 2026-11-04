import type { Feature } from '../../core/registry'
import { OwnershipPage } from './OwnershipPage'
import { OwnershipSection } from './OwnershipSection'

export const ownershipFeature: Feature = {
  name: 'ownership',
  requiresModule: 'ownership',
  signals: { 'ownership.changed': ['ownership'] },
  nav: [{ id: 'ownership', section: 'personal', labelKey: 'nav.ownership', to: '/ownership' }],
  routes: [{ path: '/ownership', element: <OwnershipPage /> }],
  // Under every object: the section hides itself for kinds that cannot have an owner.
  objectSections: [
    {
      id: 'ownership',
      render: ({ nodeId, objectType, objectId }) => (
        <OwnershipSection nodeId={nodeId} objectType={objectType} objectId={objectId} />
      ),
    },
  ],
}

import type { Feature } from '../../core/registry'
import { ImageUpdatesTab } from './ImageUpdatesTab'

export const imageUpdatesFeature: Feature = {
  name: 'imageupdates',
  requiresModule: 'imageupdates',
  signals: { 'image-updates.changed': ['image-updates'] },
  nodeTabs: [
    {
      id: 'image-updates',
      labelKey: 'imageupdates:tab',
      nodeTypes: ['docker', 'full'],
      requiresRight: 'imageupdates.read',
      render: ({ nodeId }) => <ImageUpdatesTab nodeId={nodeId} />,
    },
  ],
}

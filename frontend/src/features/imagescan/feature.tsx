import type { Feature } from '../../core/registry'
import { ImageScanTab } from './ImageScanTab'

export const imageScanFeature: Feature = {
  name: 'imagescan',
  requiresModule: 'imagescan',
  signals: { 'imagescan.changed': ['imagescan'] },
  nodeTabs: [
    {
      id: 'imagescan',
      labelKey: 'imagescan:tab',
      nodeTypes: ['docker', 'full'],
      requiresRight: 'imagescan.read',
      render: ({ nodeId }) => <ImageScanTab nodeId={nodeId} />,
    },
  ],
}

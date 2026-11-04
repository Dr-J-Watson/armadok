import type { Feature } from '../../core/registry'
import { TrafficTab } from './TrafficTab'

export const trafficFeature: Feature = {
  name: 'traffic',
  requiresModule: 'traffic',
  nodeTabs: [
    {
      id: 'traffic',
      labelKey: 'traffic:tab',
      nodeTypes: ['network', 'full'],
      requiresRight: 'traffic.read',
      render: ({ nodeId }) => <TrafficTab nodeId={nodeId} />,
    },
  ],
}

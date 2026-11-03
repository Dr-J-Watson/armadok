import type { Feature } from '../../core/registry'
import { WorkerTab } from './WorkerTab'

export const workersFeature: Feature = {
  name: 'workers',
  signals: { 'workers.changed': ['worker', 'nodes'] },
  requiresModule: 'workers',
  nodeTabs: [
    {
      id: 'worker',
      labelKey: 'workers:tab',
      render: ({ nodeId }) => <WorkerTab nodeId={nodeId} />,
    },
  ],
}

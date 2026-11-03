import type { Feature } from '../../core/registry'
import { ContainersTab } from './ContainersTab'

export const dockerFeature: Feature = {
  name: 'docker',
  requiresModule: 'docker',
  nodeTabs: [
    {
      id: 'containers',
      labelKey: 'docker:tab',
      nodeTypes: ['docker', 'full'],
      render: ({ nodeId }) => <ContainersTab nodeId={nodeId} />,
    },
  ],
}

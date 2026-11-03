import type { Feature } from '../../core/registry'
import { ContainersTab } from './ContainersTab'
import { ResourcesTab } from './ResourcesTab'

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
    {
      id: 'docker-resources',
      labelKey: 'docker:resourcesTab',
      nodeTypes: ['docker', 'full'],
      render: ({ nodeId }) => <ResourcesTab nodeId={nodeId} />,
    },
  ],
}

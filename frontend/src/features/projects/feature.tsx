import type { Feature } from '../../core/registry'
import { ProjectsTab } from './ProjectsTab'

export const projectsFeature: Feature = {
  name: 'projects',
  requiresModule: 'projects',
  nodeTabs: [
    {
      id: 'projects',
      labelKey: 'projects:tab',
      nodeTypes: ['docker', 'full'],
      render: ({ nodeId }) => <ProjectsTab nodeId={nodeId} />,
    },
  ],
}

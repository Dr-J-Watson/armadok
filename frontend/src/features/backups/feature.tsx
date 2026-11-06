import type { Feature } from '../../core/registry'
import { BackupsTab } from './BackupsTab'

export const backupsFeature: Feature = {
  name: 'backups',
  requiresModule: 'backups',
  signals: { 'backups.changed': ['backups'] },
  nodeTabs: [
    {
      id: 'backups',
      labelKey: 'backups:tab',
      area: 'docker',
      icon: 'backup',
      nodeTypes: ['docker', 'full'],
      render: ({ nodeId }) => <BackupsTab nodeId={nodeId} />,
    },
  ],
}

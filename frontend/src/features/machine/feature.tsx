import type { Feature } from '../../core/registry'
import { MachineTab } from './MachineTab'

export const machineFeature: Feature = {
  name: 'machine',
  requiresModule: 'machine',
  nodeTabs: [
    {
      id: 'machine',
      labelKey: 'machine:tab',
      render: ({ nodeId }) => <MachineTab nodeId={nodeId} />,
    },
  ],
}

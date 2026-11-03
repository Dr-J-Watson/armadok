import type { Feature } from '../../core/registry'
import { ScheduleTab } from './ScheduleTab'

export const scheduleFeature: Feature = {
  name: 'schedule',
  requiresModule: 'schedule',
  signals: { 'schedule.changed': ['schedule'] },
  nodeTabs: [
    {
      id: 'schedule',
      labelKey: 'schedule:tab',
      nodeTypes: ['docker', 'full'],
      render: ({ nodeId }) => <ScheduleTab nodeId={nodeId} />,
    },
  ],
}

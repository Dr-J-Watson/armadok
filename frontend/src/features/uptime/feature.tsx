import type { Feature } from '../../core/registry'
import { UptimeTab } from './UptimeTab'

export const uptimeFeature: Feature = {
  name: 'uptime',
  requiresModule: 'uptime',
  signals: { 'uptime.changed': ['uptime'] },
  nodeTabs: [
    {
      id: 'uptime',
      labelKey: 'uptime:tab',
      nodeTypes: ['network', 'full'],
      render: ({ nodeId }) => <UptimeTab nodeId={nodeId} />,
    },
  ],
}

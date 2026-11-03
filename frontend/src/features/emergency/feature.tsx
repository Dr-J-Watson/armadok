import type { Feature } from '../../core/registry'
import { EmergencyBanner } from './EmergencyBanner'
import { EmergencyTab } from './EmergencyTab'

export const emergencyFeature: Feature = {
  name: 'emergency',
  requiresModule: 'emergency',
  signals: { 'emergency.changed': ['emergency', 'emergency-active'] },
  headerWidgets: [{ id: 'emergency-banner', render: () => <EmergencyBanner /> }],
  nodeTabs: [
    {
      id: 'emergency',
      labelKey: 'emergency:tab',
      render: ({ nodeId }) => <EmergencyTab nodeId={nodeId} />,
    },
  ],
}

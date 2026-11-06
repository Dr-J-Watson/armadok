import type { Feature } from '../../core/registry'
import { TraefikTab } from './TraefikTab'

export const traefikFeature: Feature = {
  name: 'traefik',
  requiresModule: 'traefik',
  nodeTabs: [
    {
      id: 'traefik',
      labelKey: 'traefik:tab',
      area: 'network',
      icon: 'route',
      nodeTypes: ['network', 'full'],
      render: ({ nodeId }) => <TraefikTab nodeId={nodeId} />,
    },
  ],
}

import type { Feature } from '../../core/registry'
import { MiddlewaresPage } from './MiddlewaresPage'
import { SitesPage } from './SitesPage'
import { TraefikAdvanced } from './TraefikTab'

export const traefikFeature: Feature = {
  name: 'traefik',
  requiresModule: 'traefik',
  nodeTabs: [
    {
      id: 'traefik',
      labelKey: 'traefik:tabSites',
      area: 'network',
      icon: 'route',
      nodeTypes: ['network', 'full'],
      render: ({ nodeId }) => <SitesPage nodeId={nodeId} />,
    },
    {
      id: 'middlewares',
      labelKey: 'traefik:tabMiddlewares',
      area: 'network',
      icon: 'layers',
      nodeTypes: ['network', 'full'],
      render: ({ nodeId }) => <MiddlewaresPage nodeId={nodeId} />,
    },
    {
      id: 'traefik-advanced',
      labelKey: 'traefik:tabAdvanced',
      area: 'network',
      icon: 'wrench',
      nodeTypes: ['network', 'full'],
      render: ({ nodeId }) => <TraefikAdvanced nodeId={nodeId} />,
    },
  ],
}

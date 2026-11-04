import type { Feature } from '../../core/registry'
import { DnsPage } from './DnsPage'
import { DomainPage } from './DomainPage'
import { NodeDnsTab } from './NodeDnsTab'

export const dnsFeature: Feature = {
  name: 'dns',
  signals: { 'dns.changed': ['dns-domains', 'dns-records', 'dns-accounts', 'dns-report'] },
  requiresModule: 'dns',
  nav: [{ id: 'dns', section: 'main', labelKey: 'nav.dns', to: '/dns' }],
  routes: [
    { path: '/dns', element: <DnsPage /> },
    { path: '/dns/:domainId', element: <DomainPage /> },
  ],
  nodeTabs: [
    {
      id: 'dns',
      labelKey: 'dns:tab',
      nodeTypes: ['network', 'full'],
      render: ({ nodeId }) => <NodeDnsTab nodeId={nodeId} />,
    },
  ],
}

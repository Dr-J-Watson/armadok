import type { Feature } from '../../core/registry'
import { CertsTab } from './CertsTab'

export const certsFeature: Feature = {
  name: 'certs',
  requiresModule: 'certs',
  nodeTabs: [
    {
      id: 'certs',
      labelKey: 'certs:tab',
      area: 'network',
      icon: 'lock',
      nodeTypes: ['network', 'full'],
      render: ({ nodeId }) => <CertsTab nodeId={nodeId} />,
    },
  ],
}

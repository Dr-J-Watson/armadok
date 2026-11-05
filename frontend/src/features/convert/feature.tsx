import type { Feature } from '../../core/registry'
import { ConvertPage } from './ConvertPage'
import { FromContainers } from './FromContainers'

export const convertFeature: Feature = {
  name: 'convert',
  requiresModule: 'convert',
  nav: [{ id: 'convert', icon: 'wand', section: 'main', labelKey: 'nav.convert', to: '/convert' }],
  routes: [{ path: '/convert', element: <ConvertPage /> }],
  nodeTabs: [
    {
      id: 'convert-containers',
      labelKey: 'convert:fromContainers.tab',
      nodeTypes: ['docker', 'full'],
      render: ({ nodeId }) => <FromContainers nodeId={nodeId} />,
    },
  ],
}

import type { Feature } from '../../core/registry'
import { NodePage } from './NodePage'
import { NodesPage } from './NodesPage'

export const nodesFeature: Feature = {
  name: 'nodes',
  signals: { 'nodes.changed': ['nodes'] },
  requiresModule: 'nodes',
  nav: [{ id: 'nodes', icon: 'server', section: 'main', labelKey: 'nav.nodes', to: '/nodes' }],
  routes: [
    { path: '/nodes', element: <NodesPage /> },
    { path: '/nodes/:nodeId', element: <NodePage /> },
  ],
}

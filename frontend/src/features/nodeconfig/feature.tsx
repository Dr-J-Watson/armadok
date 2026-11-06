import type { Feature } from '../../core/registry'
import { NodeSettings } from './NodeSettings'

// A tab of a node's settings. No right is asked here: owners hold theirs on the node only, and
// the server answers for everyone (a reader sees the values, only a writer may change them).
export const nodeconfigFeature: Feature = {
  name: 'nodeconfig',
  requiresModule: 'nodeconfig',
  nodeTabs: [
    {
      id: 'nodeconfig',
      labelKey: 'nodeconfig:tab',
      render: ({ nodeId }) => <NodeSettings nodeId={nodeId} />,
    },
  ],
}

import type { Feature } from '../../core/registry'
import { MembersTab } from './MembersTab'

export const membersFeature: Feature = {
  name: 'members',
  requiresModule: 'members',
  signals: { 'members.changed': ['members', 'member-roles'] },
  nodeTabs: [
    {
      id: 'members',
      labelKey: 'members:tab',
      render: ({ nodeId }) => <MembersTab nodeId={nodeId} />,
    },
  ],
}

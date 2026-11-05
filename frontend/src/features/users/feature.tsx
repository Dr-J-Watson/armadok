import type { Feature } from '../../core/registry'
import { UserPage } from './UserPage'
import { UsersPage } from './UsersPage'

export const usersFeature: Feature = {
  name: 'users',
  signals: { 'users.changed': ['users', 'grants', 'effective', 'node-access'] },
  requiresModule: 'users',
  nav: [{ id: 'users', icon: 'users', section: 'access', labelKey: 'nav.users', to: '/users', requiresRight: 'users.read' }],
  routes: [
    { path: '/users', element: <UsersPage /> },
    { path: '/users/:userId', element: <UserPage /> },
  ],
}

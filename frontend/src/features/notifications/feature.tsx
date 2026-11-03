import type { Feature } from '../../core/registry'
import { Bell } from './Bell'
import { NotificationsPage } from './NotificationsPage'

export const notificationsFeature: Feature = {
  name: 'notifications',
  requiresModule: 'notifications',
  routes: [{ path: '/notifications', element: <NotificationsPage /> }],
  headerWidgets: [{ id: 'bell', render: () => <Bell /> }],
}

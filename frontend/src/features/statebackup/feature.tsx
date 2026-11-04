import type { Feature } from '../../core/registry'
import { StateBackupsPage } from './StateBackupsPage'

export const stateBackupFeature: Feature = {
  name: 'statebackup',
  requiresModule: 'statebackup',
  signals: { 'statebackups.changed': ['statebackups'] },
  nav: [{ id: 'statebackups', section: 'admin', labelKey: 'nav.stateBackups', to: '/state-backups', requiresRight: 'state_backup.manage' }],
  routes: [{ path: '/state-backups', element: <StateBackupsPage /> }],
}

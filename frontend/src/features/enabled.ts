// The single list of enabled features. Adding a feature = create its folder and add it here.

import type { Feature } from '../core/registry'
import { accountFeature } from './account/feature'
import { auditFeature } from './audit/feature'
import { backupsFeature } from './backups/feature'
import { certsFeature } from './certs/feature'
import { dnsFeature } from './dns/feature'
import { emergencyFeature } from './emergency/feature'
import { dockerFeature } from './docker/feature'
import { integrationsFeature } from './integrations/feature'
import { machineFeature } from './machine/feature'
import { membersFeature } from './members/feature'
import { nodeAdminFeature } from './nodeadmin/feature'
import { nodesFeature } from './nodes/feature'
import { notesFeature } from './notes/feature'
import { notificationsFeature } from './notifications/feature'
import { projectsFeature } from './projects/feature'
import { relationsFeature } from './relations/feature'
import { serviceAccountsFeature } from './service_accounts/feature'
import { startFeature } from './start/feature'
import { rolesFeature } from './roles/feature'
import { scheduleFeature } from './schedule/feature'
import { uptimeFeature } from './uptime/feature'
import { stateBackupFeature } from './statebackup/feature'
import { traefikFeature } from './traefik/feature'
import { usersFeature } from './users/feature'
import { workersFeature } from './workers/feature'

export const features: Feature[] = [
  startFeature,
  nodesFeature,
  workersFeature,
  dockerFeature,
  machineFeature,
  projectsFeature,
  traefikFeature,
  relationsFeature,
  usersFeature,
  rolesFeature,
  membersFeature,
  nodeAdminFeature,
  emergencyFeature,
  integrationsFeature,
  serviceAccountsFeature,
  auditFeature,
  notesFeature,
  notificationsFeature,
  dnsFeature,
  certsFeature,
  uptimeFeature,
  scheduleFeature,
  backupsFeature,
  stateBackupFeature,
  accountFeature,
]

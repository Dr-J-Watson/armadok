// The single list of enabled features. Adding a feature = create its folder and add it here.

import type { Feature } from '../core/registry'
import { accountFeature } from './account/feature'
import { auditFeature } from './audit/feature'
import { certsFeature } from './certs/feature'
import { dnsFeature } from './dns/feature'
import { dockerFeature } from './docker/feature'
import { integrationsFeature } from './integrations/feature'
import { membersFeature } from './members/feature'
import { nodeAdminFeature } from './nodeadmin/feature'
import { nodesFeature } from './nodes/feature'
import { notesFeature } from './notes/feature'
import { notificationsFeature } from './notifications/feature'
import { projectsFeature } from './projects/feature'
import { relationsFeature } from './relations/feature'
import { serviceAccountsFeature } from './service_accounts/feature'
import { rolesFeature } from './roles/feature'
import { traefikFeature } from './traefik/feature'
import { usersFeature } from './users/feature'
import { workersFeature } from './workers/feature'

export const features: Feature[] = [
  nodesFeature,
  workersFeature,
  dockerFeature,
  projectsFeature,
  traefikFeature,
  relationsFeature,
  usersFeature,
  rolesFeature,
  membersFeature,
  nodeAdminFeature,
  integrationsFeature,
  serviceAccountsFeature,
  auditFeature,
  notesFeature,
  notificationsFeature,
  dnsFeature,
  certsFeature,
  accountFeature,
]

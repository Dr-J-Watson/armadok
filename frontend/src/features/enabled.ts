// The single list of enabled features. Adding a feature = create its folder and add it here.

import type { Feature } from '../core/registry'
import { accountFeature } from './account/feature'
import { auditFeature } from './audit/feature'
import { dnsFeature } from './dns/feature'
import { dockerFeature } from './docker/feature'
import { nodesFeature } from './nodes/feature'
import { notesFeature } from './notes/feature'
import { notificationsFeature } from './notifications/feature'
import { projectsFeature } from './projects/feature'
import { relationsFeature } from './relations/feature'
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
  auditFeature,
  notesFeature,
  notificationsFeature,
  dnsFeature,
  accountFeature,
]

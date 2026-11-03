// The single list of enabled features. Adding a feature = create its folder and add it here.

import type { Feature } from '../core/registry'
import { dockerFeature } from './docker/feature'
import { nodesFeature } from './nodes/feature'
import { relationsFeature } from './relations/feature'
import { workersFeature } from './workers/feature'

export const features: Feature[] = [nodesFeature, workersFeature, dockerFeature, relationsFeature]

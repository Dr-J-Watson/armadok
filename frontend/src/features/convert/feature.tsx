import type { Feature } from '../../core/registry'
import { DockerRunSource } from './DockerRunSource'
import { FromContainers } from './FromContainers'

// The converters are ways to start a project, not a place of their own.
export const convertFeature: Feature = {
  name: 'convert',
  requiresModule: 'convert',
  projectSources: [
    { id: 'docker-run', labelKey: 'convert:source.dockerRun', render: (p) => <DockerRunSource {...p} /> },
    { id: 'containers', labelKey: 'convert:source.containers', render: (p) => <FromContainers {...p} /> },
  ],
}

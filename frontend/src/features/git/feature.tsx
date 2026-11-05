import type { Feature } from '../../core/registry'
import { GitPanel } from './GitPanel'
import { GitSource } from './GitSource'

// Projects that come from a Git repository: start one from it, follow and update it.
export const gitFeature: Feature = {
  name: 'git',
  requiresModule: 'git',
  projectSources: [{ id: 'git', labelKey: 'git:source.label', render: (p) => <GitSource {...p} /> }],
  projectPanels: [{ id: 'git', render: (p) => <GitPanel {...p} /> }],
}

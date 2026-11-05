import type { Feature } from '../../core/registry'
import { KeepAsTemplate } from './KeepAsTemplate'
import { TemplateSource } from './TemplateSource'

// Templates are a way to start a project, and a way to keep one for later.
export const templatesFeature: Feature = {
  name: 'templates',
  requiresModule: 'templates',
  projectSources: [{ id: 'template', labelKey: 'templates:source', render: (p) => <TemplateSource {...p} /> }],
  projectDraftActions: [{ id: 'keep', render: (p) => <KeepAsTemplate {...p} /> }],
}

import type { Feature } from '../../core/registry'
import { NotesPage } from './NotesPage'
import { NotesPanel } from './NotesPanel'

export const notesFeature: Feature = {
  name: 'notes',
  requiresModule: 'notes',
  nav: [{ id: 'notes', labelKey: 'nav.notes', to: '/notes' }],
  routes: [{ path: '/notes', element: <NotesPage /> }],
  // Shown under every object that has a panel: notes can be attached to anything.
  objectSections: [
    {
      id: 'notes',
      render: ({ nodeId, objectType, objectId }) => (
        <NotesPanel nodeId={nodeId} objectType={objectType} objectId={objectId} />
      ),
    },
  ],
}

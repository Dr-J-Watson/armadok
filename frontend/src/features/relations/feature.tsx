import { Suspense, lazy } from 'react'
import type { Feature } from '../../core/registry'

// The map pulls in the drawing library, so it is loaded only when the tab is opened.
const MapTab = lazy(() => import('./MapTab').then((m) => ({ default: m.MapTab })))

export const relationsFeature: Feature = {
  name: 'relations',
  requiresModule: 'relations',
  nodeTabs: [
    {
      id: 'map',
      labelKey: 'relations:tab',
      render: ({ nodeId }) => (
        <Suspense fallback={<p>…</p>}>
          <MapTab nodeId={nodeId} />
        </Suspense>
      ),
    },
  ],
}

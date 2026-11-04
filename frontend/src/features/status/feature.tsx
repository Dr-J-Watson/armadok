import type { Feature } from '../../core/registry'
import { PublicStatusPage } from './PublicStatusPage'

export const statusFeature: Feature = {
  name: 'status',
  requiresModule: 'status',
  // Opened without signing in: the address is the key.
  publicRoutes: [{ path: '/status/:slug', element: <PublicStatusPage /> }],
}

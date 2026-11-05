import type { Feature } from '../../core/registry'
import { ConvertPage } from './ConvertPage'

export const convertFeature: Feature = {
  name: 'convert',
  requiresModule: 'convert',
  nav: [{ id: 'convert', icon: 'wand', section: 'main', labelKey: 'nav.convert', to: '/convert' }],
  routes: [{ path: '/convert', element: <ConvertPage /> }],
}

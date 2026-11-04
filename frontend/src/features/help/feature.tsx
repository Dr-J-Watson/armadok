import type { Feature } from '../../core/registry'
import { HelpPage } from './HelpPage'

export const helpFeature: Feature = {
  name: 'help',
  nav: [{ id: 'help', labelKey: 'nav.help', to: '/help' }],
  routes: [{ path: '/help', element: <HelpPage /> }],
}

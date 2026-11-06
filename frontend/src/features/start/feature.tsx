import type { Feature } from '../../core/registry'
import { StartPage } from './StartPage'

// Getting started is no menu entry of its own: it is the help section of a node's settings.
export const startFeature: Feature = {
  name: 'start',
  requiresModule: 'diagnostics',
  nodeTabs: [
    {
      id: 'help',
      labelKey: 'start:tab',
      render: () => <StartPage />,
    },
  ],
  routes: [{ path: '/start', element: <StartPage /> }],
}

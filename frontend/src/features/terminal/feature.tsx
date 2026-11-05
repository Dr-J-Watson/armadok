import type { Feature } from '../../core/registry'
import { TerminalPanel } from './TerminalPanel'

// A shell in a throwaway container that sees one project folder.
export const terminalFeature: Feature = {
  name: 'terminal',
  requiresModule: 'terminal',
  projectPanels: [{ id: 'terminal', render: (p) => <TerminalPanel {...p} /> }],
}

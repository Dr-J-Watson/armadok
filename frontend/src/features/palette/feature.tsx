import type { Feature } from '../../core/registry'
import { CommandPalette } from './CommandPalette'

export const paletteFeature: Feature = {
  name: 'palette',
  headerWidgets: [{ id: 'command-palette', render: () => <CommandPalette /> }],
}

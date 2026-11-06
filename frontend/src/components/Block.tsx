import type { ReactNode } from 'react'
import { CircleHelp } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export type BlockTone = 'name' | 'when' | 'apply' | 'send' | 'https'

// One step of a builder, drawn as a coloured frame with a tab on its top edge. Stacked inside a
// `.block-flow`, the blocks are joined by a small arrow so the form reads top to bottom as a
// sentence ("when someone visits… then apply… then send to…"). The tone only picks a colour
// variable, so a theme restyles every block at once.
export function Block({
  tone,
  icon: Icon,
  title,
  help,
  children,
}: {
  tone: BlockTone
  icon: LucideIcon
  title: string
  help?: string
  children: ReactNode
}) {
  return (
    <section className={`block block-${tone}`}>
      <h4 className="block-tab">
        <Icon size={14} aria-hidden="true" />
        <span>{title}</span>
        {help && (
          <span className="block-help" title={help}>
            <CircleHelp size={14} aria-hidden="true" />
            <span className="visually-hidden">{help}</span>
          </span>
        )}
      </h4>
      {children}
    </section>
  )
}

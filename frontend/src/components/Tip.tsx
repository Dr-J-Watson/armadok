import { Info } from 'lucide-react'

// A small "i" next to a label: the explanation shows on hover and on keyboard focus, and is read
// aloud by screen readers. Used for anything that needs a sentence of context but would clutter the
// page if the sentence were always shown.
export function Tip({ text }: { text: string }) {
  return (
    <span className="tip" tabIndex={0} role="img" aria-label={text} title={text}>
      <Info size={14} aria-hidden="true" />
    </span>
  )
}

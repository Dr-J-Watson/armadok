import { X } from 'lucide-react'
import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

// A panel that slides in from the right over the page, for editing one thing without leaving the
// list behind it. Escape or a click outside closes it; the page underneath keeps its place.
export function Drawer({ title, badge, onClose, footer, children }: { title: string; badge?: string; onClose: () => void; footer?: ReactNode; children: ReactNode }) {
  const { t } = useTranslation()
  useEffect(() => {
    const escape = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [onClose])
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>
            {title}
            {badge && <span className="badge">{badge}</span>}
          </h2>
          <button type="button" className="icon-button" aria-label={t('drawer.close')} title={t('drawer.close')} onClick={onClose}>
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <div className="drawer-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </aside>
    </div>
  )
}

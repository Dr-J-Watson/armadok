import { Menu, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Logo } from '../components/Logo'
import { NavIcon } from '../components/NavIcon'
import { useCan } from '../core/auth'
import { useFeatures } from '../core/useFeatures'
import type { NavItem } from '../core/registry'
import { useRealtimeStatus } from '../core/RealtimeProvider'
import { AccountMenu } from './AccountMenu'

// The groups of the side menu, in the order they appear. A feature picks one with `section`.
const SECTIONS = ['main', 'access', 'admin', 'personal'] as const

function NavEntry({ item, onGo }: { item: NavItem; onGo: () => void }) {
  const { t } = useTranslation()
  const allowed = useCan(item.requiresRight ?? '')
  if (item.requiresRight && !allowed) return null
  return (
    <NavLink to={item.to} onClick={onGo} className="nav-link">
      <NavIcon name={item.icon} />
      <span>{t(item.labelKey)}</span>
    </NavLink>
  )
}

// The frame around every page: a side menu grouped by section (from the registry) with the
// person's own menu pinned at its foot. On a narrow screen the menu is a drawer behind a button.
export function Layout({ nav }: { nav: NavItem[] }) {
  const { t } = useTranslation()
  const live = useRealtimeStatus()
  const all = useFeatures().flatMap((f) => f.headerWidgets ?? [])
  // The search is a bar of its own; the other widgets (the bell) sit beside the name.
  const search = all.find((w) => w.id === 'command-palette')
  const widgets = all.filter((w) => w.id !== 'command-palette')
  const [open, setOpen] = useState(false)
  const location = useLocation()
  // The tab's title names the page, so several tabs of Armadok can be told apart (and a screen
  // reader announces where it went).
  const here = nav.find((n) => location.pathname === n.to || location.pathname.startsWith(`${n.to}/`))
  const pageTitle = here ? `${t(here.labelKey)} · ${t('app.name')}` : t('app.name')
  useEffect(() => {
    document.title = pageTitle
  }, [pageTitle])
  // Going to another page closes the drawer (adjusted while rendering, not in an effect).
  const [seen, setSeen] = useState(location.pathname)
  if (seen !== location.pathname) {
    setSeen(location.pathname)
    setOpen(false)
  }
  useEffect(() => {
    if (!open) return
    const escape = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [open])
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        {t('nav.skip')}
      </a>
      <header className="mobilebar">
        <span className="brand">
          <Logo size={26} />
          <strong>{t('app.name')}</strong>
        </span>
        <span className="mobilebar-end">
          {search?.render()}
          {widgets.map((widget) => (
            <span key={widget.id}>{widget.render()}</span>
          ))}
          <button type="button" className="icon-button" aria-expanded={open} aria-controls="sidebar" aria-label={t('nav.menu')} onClick={() => setOpen(!open)}>
            {open ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}
          </button>
        </span>
      </header>
      {open && <div className="scrim" onClick={() => setOpen(false)} aria-hidden="true" />}
      <aside id="sidebar" className={`sidebar${open ? ' open' : ''}`}>
        <div className="sidebar-head">
          <span className="brand">
            <Logo />
            <strong>{t('app.name')}</strong>
          </span>
          <span className="sidebar-widgets">
            {widgets.map((widget) => (
              <span key={widget.id}>{widget.render()}</span>
            ))}
          </span>
        </div>
        {search && <div className="sidebar-search">{search.render()}</div>}
        <nav aria-label={t('nav.main')}>
          {SECTIONS.map((section) => {
            const items = nav.filter((n) => (n.section ?? 'main') === section)
            if (items.length === 0) return null
            return (
              <div key={section} className="nav-group" role="group" aria-label={t(`nav.section.${section}`)}>
                <h2 className="nav-title">{t(`nav.section.${section}`)}</h2>
                {items.map((item) => (
                  <NavEntry key={item.id} item={item} onGo={() => setOpen(false)} />
                ))}
              </div>
            )
          })}
        </nav>
        <div className="sidebar-foot">
          <span className={`live live-${live}`} title={t(`live.${live}.hint`)}>
            <span className="live-dot" aria-hidden="true" />
            {t(`live.${live}.label`)}
          </span>
          <AccountMenu />
        </div>
      </aside>
      <main className="content" id="main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  )
}

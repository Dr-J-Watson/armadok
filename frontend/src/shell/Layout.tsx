import { useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useAuth, useCan } from '../core/auth'
import { useFeatures } from '../core/useFeatures'
import type { NavItem } from '../core/registry'
import { useRealtimeStatus } from '../core/RealtimeProvider'
import { Preferences } from './Preferences'

// The groups of the side menu, in the order they appear. A feature picks one with `section`.
const SECTIONS = ['main', 'access', 'admin', 'personal'] as const

function NavEntry({ item }: { item: NavItem }) {
  const { t } = useTranslation()
  const allowed = useCan(item.requiresRight ?? '')
  if (item.requiresRight && !allowed) return null
  return <NavLink to={item.to}>{t(item.labelKey)}</NavLink>
}

// The frame around every page: a side menu grouped by section (from the registry), and at its
// foot the preferences, the account and the state of the live connection. On a narrow screen
// the menu folds behind a button.
export function Layout({ nav }: { nav: NavItem[] }) {
  const { t } = useTranslation()
  const { state, logout } = useAuth()
  const live = useRealtimeStatus()
  const widgets = useFeatures().flatMap((f) => f.headerWidgets ?? [])
  const [open, setOpen] = useState(false)
  // Going to another page closes the folded menu.
  const location = useLocation()
  const [seen, setSeen] = useState(location.pathname)
  if (seen !== location.pathname) {
    setSeen(location.pathname)
    setOpen(false)
  }
  return (
    <div className="shell">
      <header className="mobilebar">
        <strong className="brand">{t('app.name')}</strong>
        <button type="button" aria-expanded={open} aria-controls="sidebar" onClick={() => setOpen(!open)}>
          {t('nav.menu')}
        </button>
      </header>
      <aside id="sidebar" className={`sidebar${open ? ' open' : ''}`}>
        <strong className="brand">{t('app.name')}</strong>
        <nav aria-label={t('nav.main')}>
          {SECTIONS.map((section) => {
            const items = nav.filter((n) => (n.section ?? 'main') === section)
            if (items.length === 0) return null
            return (
              <div key={section} className="nav-group" role="group" aria-label={t(`nav.section.${section}`)}>
                <h2 className="nav-title">{t(`nav.section.${section}`)}</h2>
                {items.map((item) => (
                  <NavEntry key={item.id} item={item} />
                ))}
              </div>
            )
          })}
        </nav>
        <div className="sidebar-foot">
          {widgets.map((widget) => (
            <span key={widget.id}>{widget.render()}</span>
          ))}
          <span className={`badge ${live === 'live' ? 'ok' : 'off'}`} title={t(`live.${live}.hint`)}>
            {t(`live.${live}.label`)}
          </span>
          <Preferences />
          {state.status === 'authenticated' && (
            <div className="account-line">
              <span className="user">{state.user.username}</span>
              <button type="button" onClick={() => void logout()}>
                {t('auth:logout')}
              </button>
            </div>
          )}
        </div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}

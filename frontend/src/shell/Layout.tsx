import { NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../core/auth'
import type { NavItem } from '../core/registry'
import { Preferences } from './Preferences'

// The frame around every page: navigation (from the registry), preferences, account.
export function Layout({ nav }: { nav: NavItem[] }) {
  const { t } = useTranslation()
  const { state, logout } = useAuth()
  return (
    <div className="shell">
      <header className="topbar">
        <strong className="brand">{t('app.name')}</strong>
        <nav aria-label={t('nav.main')}>
          {nav.map((item) => (
            <NavLink key={item.id} to={item.to}>
              {t(item.labelKey)}
            </NavLink>
          ))}
        </nav>
        <div className="topbar-end">
          <Preferences />
          {state.status === 'authenticated' && (
            <>
              <span className="user">{state.user.username}</span>
              <button type="button" onClick={() => void logout()}>
                {t('auth:logout')}
              </button>
            </>
          )}
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}

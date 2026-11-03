import { NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useAuth, useCan } from '../core/auth'
import { useFeatures } from '../core/useFeatures'
import type { NavItem } from '../core/registry'
import { useRealtimeStatus } from '../core/RealtimeProvider'
import { Preferences } from './Preferences'

function NavEntry({ item }: { item: NavItem }) {
  const { t } = useTranslation()
  const allowed = useCan(item.requiresRight ?? '')
  if (item.requiresRight && !allowed) return null
  return <NavLink to={item.to}>{t(item.labelKey)}</NavLink>
}

// The frame around every page: navigation (from the registry), preferences, account.
export function Layout({ nav }: { nav: NavItem[] }) {
  const { t } = useTranslation()
  const { state, logout } = useAuth()
  const live = useRealtimeStatus()
  const widgets = useFeatures().flatMap((f) => f.headerWidgets ?? [])
  return (
    <div className="shell">
      <header className="topbar">
        <strong className="brand">{t('app.name')}</strong>
        <nav aria-label={t('nav.main')}>
          {nav.map((item) => (
            <NavEntry key={item.id} item={item} />
          ))}
        </nav>
        <div className="topbar-end">
          {widgets.map((widget) => (
            <span key={widget.id}>{widget.render()}</span>
          ))}
          <span className={`badge ${live === 'live' ? 'ok' : 'off'}`} title={t(`live.${live}.hint`)}>
            {t(`live.${live}.label`)}
          </span>
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

import { ChevronsUpDown, LogOut, User } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { NavLink } from 'react-router-dom'
import { useAuth } from '../core/auth'
import { leaveThisBrowser } from '../core/push'
import { Preferences } from './Preferences'

// The person at the foot of the side menu. A click opens the places that concern them alone:
// their account, language and look, and signing out. Closes on a click outside or Escape.
export function AccountMenu() {
  const { t } = useTranslation()
  const { state, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const outside = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const escape = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [open])
  if (state.status !== 'authenticated') return null
  const name = state.user.username
  return (
    <div className="account-menu" ref={box}>
      {open && (
        <div className="account-pop" role="menu" aria-label={t('nav.account')}>
          <Preferences />
          <NavLink to="/account" className="menu-item" role="menuitem" onClick={() => setOpen(false)}>
            <User size={16} aria-hidden="true" /> {t('nav.account')}
          </NavLink>
          <button
            type="button"
            className="menu-item"
            role="menuitem"
            onClick={() => void leaveThisBrowser(true).then(() => logout())}
          >
            <LogOut size={16} aria-hidden="true" /> {t('auth:logout')}
          </button>
        </div>
      )}
      <button type="button" className="account-button" aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen(!open)}>
        <span className="avatar" aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span>
        <span className="account-name">{name}</span>
        <ChevronsUpDown size={16} aria-hidden="true" className="muted" />
      </button>
    </div>
  )
}

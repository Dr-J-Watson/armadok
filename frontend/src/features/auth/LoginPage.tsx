import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../../core/api'
import { useAuth } from '../../core/auth'
import { AuthLayout } from './AuthLayout'
import { Problem } from '../../components/Problem'

export function LoginPage() {
  const { t } = useTranslation(['auth', 'errors'])
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [totp, setTotp] = useState('')
  const [needsCode, setNeedsCode] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, password, needsCode ? totp : undefined)
    } catch (caught) {
      // The server asks for the second factor only after the password was right.
      if (caught instanceof ApiError && caught.code === 'auth.totp_required') setNeedsCode(true)
      else setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthLayout title={t('auth:login.title')}>
      <form onSubmit={submit} className="form">
        <label htmlFor="username">{t('auth:username')}</label>
        <input
          id="username"
          autoComplete="username"
          autoFocus
          required
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <label htmlFor="password">{t('auth:password')}</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {needsCode && (
          <>
            <label htmlFor="totp">{t('auth:totp.label')}</label>
            <input
              id="totp"
              autoComplete="one-time-code"
              inputMode="numeric"
              required
              autoFocus
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
            />
            <p className="hint">{t('auth:totp.hint')}</p>
          </>
        )}
        {error !== null && (
          <Problem error={error} />
        )}
        <button type="submit" className="primary" disabled={busy}>
          {t('auth:login.submit')}
        </button>
      </form>
    </AuthLayout>
  )
}

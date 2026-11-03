import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../../core/auth'
import { errorMessage } from '../../core/errors'
import { AuthLayout } from './AuthLayout'

const MIN_PASSWORD = 12

export function SetupPage() {
  const { t } = useTranslation(['auth', 'errors'])
  const { setup } = useAuth()
  const [code, setCode] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [mismatch, setMismatch] = useState(false)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setMismatch(password !== confirm)
    if (password !== confirm) return
    setBusy(true)
    setError(null)
    try {
      await setup(code.trim(), username, password)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthLayout title={t('auth:setup.title')}>
      <p>{t('auth:setup.intro')}</p>
      <form onSubmit={submit} className="form">
        <label htmlFor="code">{t('auth:setup.code')}</label>
        <input
          id="code"
          required
          autoComplete="off"
          value={code}
          onChange={(e) => setCode(e.target.value)}
        />
        <p className="hint">{t('auth:setup.codeHint')}</p>
        <label htmlFor="username">{t('auth:username')}</label>
        <input
          id="username"
          required
          minLength={3}
          pattern="[A-Za-z0-9_.\-]{3,64}"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <label htmlFor="password">{t('auth:password')}</label>
        <input
          id="password"
          type="password"
          required
          minLength={MIN_PASSWORD}
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <p className="hint">{t('auth:setup.passwordHint', { count: MIN_PASSWORD })}</p>
        <label htmlFor="confirm">{t('auth:setup.confirm')}</label>
        <input
          id="confirm"
          type="password"
          required
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
        {mismatch && (
          <p role="alert" className="error">
            {t('auth:setup.mismatch')}
          </p>
        )}
        {error !== null && (
          <p role="alert" className="error">
            {errorMessage(t, error)}
          </p>
        )}
        <button type="submit" className="primary" disabled={busy}>
          {t('auth:setup.submit')}
        </button>
      </form>
    </AuthLayout>
  )
}

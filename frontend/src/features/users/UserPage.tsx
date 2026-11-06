import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { api } from '../../core/api'
import { useCan } from '../../core/auth'
import { errorMessage } from '../../core/errors'
import { EffectiveSection } from '../roles/EffectiveSection'
import { GrantsSection } from './GrantsSection'
import type { Account } from './UsersPage'
import { Problem } from '../../components/Problem'
import { useConfirm } from '../../components/Confirm'

export function UserPage() {
  const { t } = useTranslation(['users', 'errors'])
  const { userId = '' } = useParams()
  const queryClient = useQueryClient()
  const ask = useConfirm()
  const canUpdate = useCan('users.update')
  const canReset = useCan('users.reset_password')
  const canReset2fa = useCan('users.reset_2fa')
  const users = useQuery({ queryKey: ['users'], queryFn: () => api.get<Account[]>('/users') })
  const [password, setPassword] = useState('')
  const [done, setDone] = useState<string | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['users'] })
  const toggle = useMutation({
    mutationFn: (active: boolean) => api.patch(`/users/${userId}`, { is_active: active }),
    onSuccess: refresh,
  })
  const reset = useMutation({
    mutationFn: () => api.patch(`/users/${userId}`, { password }),
    onSuccess: () => {
      setPassword('')
      setDone(t('users:account.passwordChanged'))
    },
  })
  const reset2fa = useMutation({
    mutationFn: () => api.del(`/users/${userId}/2fa`),
    onSuccess: () => setDone(t('users:account.twofaReset')),
  })

  const account = users.data?.find((u) => u.id === userId)
  if (users.isPending) return <p className="loading">{t('loading')}</p>
  if (!account)
    return (
      <>
        <p role="alert" className="error">
          {users.isError ? errorMessage(t, users.error) : t('errors:common.not_found')}
        </p>
        <Link to="/users">{t('users:back')}</Link>
      </>
    )

  function submitPassword(event: FormEvent) {
    event.preventDefault()
    setDone(null)
    reset.mutate()
  }
  const failed = toggle.error ?? reset.error ?? reset2fa.error

  return (
    <>
      <nav className="crumbs">
        <Link to="/users">{t('users:back')}</Link>
      </nav>
      <header className="page-head">
        <span className="avatar" aria-hidden="true">{account.username.slice(0, 1).toUpperCase()}</span>
        <div>
          <h1>
            {account.username}
            {account.kind === 'service' && <span className="badge">{t('users:service')}</span>}
            {!account.is_active && <span className="badge off">{t('users:disabled')}</span>}
          </h1>
          <p className="hint">{t('users:account.since', { date: new Date(account.created_at).toLocaleDateString() })}</p>
        </div>
      </header>
      {failed != null && (
        <Problem error={failed} />
      )}
      {done && (
        <p role="status" className="ok-text">
          {done}
        </p>
      )}

      {canUpdate && (
        <div className="actions">
          <button
            type="button"
            className={account.is_active ? 'danger-button' : undefined}
            title={account.is_active ? t('users:account.disableTip') : undefined}
            onClick={async () => {
              if (!account.is_active || (await ask(t('users:account.disableConfirm', { name: account.username })))) toggle.mutate(!account.is_active)
            }}
          >
            {account.is_active ? t('users:account.disable') : t('users:account.enable')}
          </button>
          {canReset2fa && (
            <button
              type="button"
              title={t('users:account.resetTwofaTip')}
              onClick={async () => {
                if (await ask(t('users:account.resetTwofaConfirm', { name: account.username }))) reset2fa.mutate()
              }}
            >
              {t('users:account.resetTwofa')}
            </button>
          )}
        </div>
      )}
      {canUpdate && canReset && (
        <form className="form" onSubmit={submitPassword}>
          <label htmlFor="reset-password">{t('users:account.newPassword')}</label>
          <input
            id="reset-password"
            type="password"
            required
            minLength={12}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <p className="hint">{t('users:account.resetNote')}</p>
          <button type="submit" disabled={reset.isPending}>
            {t('users:account.resetPassword')}
          </button>
        </form>
      )}

      <GrantsSection userId={userId} />
      <EffectiveSection userId={userId} />
    </>
  )
}

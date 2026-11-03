import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { useCan } from '../../core/auth'
import { errorMessage } from '../../core/errors'

export interface Account {
  id: string
  username: string
  is_active: boolean
  created_at: string
  kind?: 'person' | 'service'
}

export function UsersPage() {
  const { t } = useTranslation(['users', 'errors'])
  const queryClient = useQueryClient()
  const canCreate = useCan('users.create')
  const users = useQuery({ queryKey: ['users'], queryFn: () => api.get<Account[]>('/users') })
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const create = useMutation({
    mutationFn: () => api.post<Account>('/users', { username, password }),
    onSuccess: () => {
      setUsername('')
      setPassword('')
      return queryClient.invalidateQueries({ queryKey: ['users'] })
    },
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  return (
    <>
      <h1>{t('users:title')}</h1>
      {users.isError && (
        <p role="alert" className="error">
          {errorMessage(t, users.error)}
        </p>
      )}
      <ul className="cards">
        {users.data?.map((account) => (
          <li key={account.id}>
            <Link to={`/users/${account.id}`}>
              <strong>{account.username}</strong>
              {account.kind === 'service' && (
                <span className="badge" title={t('users:serviceHint')}>
                  {'⚙ '}
                  {t('users:service')}
                </span>
              )}
              {!account.is_active && <span className="badge off">{t('users:disabled')}</span>}
            </Link>
          </li>
        ))}
      </ul>

      {canCreate && (
        <section>
          <h2>{t('users:create.title')}</h2>
          <p>{t('users:create.explain')}</p>
          <form className="form" onSubmit={submit}>
            <label htmlFor="new-username">{t('users:create.username')}</label>
            <input
              id="new-username"
              required
              minLength={3}
              pattern="[A-Za-z0-9_.\-]{3,64}"
              autoComplete="off"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
            <label htmlFor="new-password">{t('users:create.password')}</label>
            <input
              id="new-password"
              type="password"
              required
              minLength={12}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <p className="hint">{t('users:create.passwordHint')}</p>
            {create.isError && (
              <p role="alert" className="error">
                {errorMessage(t, create.error)}
              </p>
            )}
            <button type="submit" className="primary" disabled={create.isPending}>
              {t('users:create.submit')}
            </button>
          </form>
        </section>
      )}
    </>
  )
}

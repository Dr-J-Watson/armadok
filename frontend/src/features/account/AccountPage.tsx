import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import type { User } from '../../core/auth'
import { useModules } from '../../core/modules'
import { ApiKeys } from './ApiKeys'
import { TwoFactor } from './TwoFactor'
import { Problem } from '../../components/Problem'

export function AccountPage() {
  const { t } = useTranslation(['account', 'errors'])
  const modules = useModules()
  // Fresh from the server (not from the login state): 2FA can change while this page is open.
  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<User>('/auth/me') })

  if (me.isPending) return <p className="loading">{t('loading')}</p>
  if (me.isError)
    return (
      <Problem error={me.error} />
    )
  return (
    <>
      <header className="page-head">
        <div>
          <h1>{t('account:title')}</h1>
          <p className="hint">
            {t('account:signedInAs')} <strong>{me.data.username}</strong>
            {me.data.totp_enabled ? (
              <span className="badge ok">{t('account:twoFactorOn')}</span>
            ) : (
              <span className="badge off">{t('account:twoFactorOff')}</span>
            )}
          </p>
        </div>
      </header>
      <TwoFactor me={me.data} />
      {modules?.has('apikeys') && <ApiKeys me={me.data} />}
    </>
  )
}

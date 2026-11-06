import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import type { User } from '../../core/auth'
import { QrCode } from './QrCode'
import { Problem } from '../../components/Problem'

interface Enrolment {
  secret: string
  otpauth_uri: string
}

export function TwoFactor({ me }: { me: User }) {
  const { t } = useTranslation(['account', 'errors'])
  const queryClient = useQueryClient()
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null)
  // Backup codes are shown once, in this component's memory only.
  const [backupCodes, setBackupCodes] = useState<string[] | null>(null)
  const done = () => queryClient.invalidateQueries({ queryKey: ['me'] })

  const enroll = useMutation({
    mutationFn: () => api.post<Enrolment>('/auth/2fa/enroll', { password }),
    onSuccess: (data) => {
      setEnrolment(data)
      setPassword('')
    },
  })
  const confirm = useMutation({
    mutationFn: () => api.post<{ backup_codes: string[] }>('/auth/2fa/confirm', { code }),
    onSuccess: async (data) => {
      setBackupCodes(data.backup_codes)
      setEnrolment(null)
      setCode('')
      await done()
    },
  })
  const disable = useMutation({
    mutationFn: () => api.post('/auth/2fa/disable', { password, code }),
    onSuccess: async () => {
      setPassword('')
      setCode('')
      await done()
    },
  })

  const submit = (mutation: { mutate: () => void }) => (event: FormEvent) => {
    event.preventDefault()
    mutation.mutate()
  }
  const failed = enroll.error ?? confirm.error ?? disable.error

  return (
    <section>
      <h2>{t('account:twofa.title')}</h2>
      <p>
        <span className={`badge ${me.totp_enabled ? 'ok' : 'off'}`}>
          {me.totp_enabled ? t('account:twofa.on') : t('account:twofa.off')}
        </span>
        {me.totp_enabled && (
          <> {t('account:twofa.backupLeft', { count: me.backup_codes_remaining })}</>
        )}
      </p>
      {failed != null && (
        <Problem error={failed} />
      )}

      {backupCodes && (
        <div className="notice">
          <h3>{t('account:twofa.backupTitle')}</h3>
          <p>{t('account:twofa.backupExplain')}</p>
          <pre>{backupCodes.join('\n')}</pre>
          <button type="button" onClick={() => void navigator.clipboard?.writeText(backupCodes.join('\n'))}>
            {t('account:twofa.backupCopy')}
          </button>{' '}
          <button type="button" className="primary" onClick={() => setBackupCodes(null)}>
            {t('account:twofa.backupSaved')}
          </button>
        </div>
      )}

      {!me.totp_enabled && !enrolment && (
        <form className="form" onSubmit={submit(enroll)}>
          <p>{t('account:twofa.explain')}</p>
          <label htmlFor="twofa-password">{t('account:password')}</label>
          <input
            id="twofa-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button type="submit" className="primary" disabled={enroll.isPending}>
            {t('account:twofa.enable')}
          </button>
        </form>
      )}

      {enrolment && (
        <form className="form" onSubmit={submit(confirm)}>
          <p>{t('account:twofa.scan')}</p>
          <QrCode value={enrolment.otpauth_uri} label={t('account:twofa.qrLabel')} />
          <p className="hint">{t('account:twofa.manual')}</p>
          <code className="secret">{enrolment.secret}</code>
          <label htmlFor="twofa-code">{t('account:twofa.firstCode')}</label>
          <input
            id="twofa-code"
            inputMode="numeric"
            autoComplete="one-time-code"
            required
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <button type="submit" className="primary" disabled={confirm.isPending}>
            {t('account:twofa.confirm')}
          </button>
        </form>
      )}

      {me.totp_enabled && (
        <form className="form" onSubmit={submit(disable)}>
          <p>{t('account:twofa.disableExplain')}</p>
          <label htmlFor="twofa-off-password">{t('account:password')}</label>
          <input
            id="twofa-off-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <label htmlFor="twofa-off-code">{t('account:twofa.anyCode')}</label>
          <input
            id="twofa-off-code"
            autoComplete="one-time-code"
            required
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <button type="submit" className="danger-button" disabled={disable.isPending}>
            {t('account:twofa.disable')}
          </button>
        </form>
      )}
    </section>
  )
}

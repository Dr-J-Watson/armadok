import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Plan {
  sites: string[]
  shared: string[]
}

// Pause or resume every site of a domain in one go. The plan is shown first; the DNS is not
// touched, and a site that also answers for another domain is left alone and named.
export function DomainSwitch({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const client = useQueryClient()
  const [domain, setDomain] = useState('')
  const [action, setAction] = useState<'pause' | 'resume'>('pause')
  const path = () => `/nodes/${nodeId}/traefik/domains/${encodeURIComponent(domain.trim().toLowerCase())}/${action}`
  const plan = useMutation({ mutationFn: () => api.post<Plan>(path(), { dry_run: true }) })
  const apply = useMutation({
    mutationFn: () => api.post<Plan>(path(), {}),
    onSuccess: () => {
      plan.reset()
      return Promise.all([
        client.invalidateQueries({ queryKey: ['traefik-managed', nodeId] }),
        client.invalidateQueries({ queryKey: ['traefik-sites', nodeId] }),
      ])
    },
  })
  const failed = plan.error ?? apply.error
  return (
    <section aria-label={t('traefik:domain.title')}>
      <h2>{t('traefik:domain.title')}</h2>
      <p className="hint">{t('traefik:domain.hint')}</p>
      <label>
        {t('traefik:domain.name')}
        <input value={domain} maxLength={253} onChange={(e) => { setDomain(e.target.value); plan.reset() }} />
      </label>{' '}
      <select aria-label={t('traefik:domain.action')} value={action} onChange={(e) => { setAction(e.target.value as 'pause' | 'resume'); plan.reset() }}>
        <option value="pause">{t('traefik:domain.pause')}</option>
        <option value="resume">{t('traefik:domain.resume')}</option>
      </select>{' '}
      <button type="button" disabled={plan.isPending || domain.trim() === ''} onClick={() => plan.mutate()}>
        {t('traefik:domain.show')}
      </button>
      {plan.data && (
        <div className="notice">
          <p>{plan.data.sites.length === 0 ? t('traefik:domain.none') : t('traefik:domain.sites', { count: plan.data.sites.length, sites: plan.data.sites.join(', ') })}</p>
          {plan.data.shared.length > 0 && <p className="hint">{t('traefik:domain.shared', { sites: plan.data.shared.join(', ') })}</p>}
          {plan.data.sites.length > 0 && (
            <button type="button" disabled={apply.isPending} onClick={() => apply.mutate()}>
              {t(action === 'pause' ? 'traefik:domain.goPause' : 'traefik:domain.goResume')}
            </button>
          )}
        </div>
      )}
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
    </section>
  )
}

import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { AccountsSection } from './AccountsSection'
import type { Provider } from './AccountsSection'
import { DomainsSection } from './DomainsSection'

// Two views of the same thing: the names you own, and the provider accounts that hold them.
export function DnsPage() {
  const { t } = useTranslation('dns')
  const [params, setParams] = useSearchParams()
  const view = params.get('view') === 'accounts' ? 'accounts' : 'domains'
  const providers = useQuery({ queryKey: ['dns-providers'], queryFn: () => api.get<Provider[]>('/dns/providers') })
  return (
    <>
      <header className="page-head">
        <div>
          <h1>{t('title')}</h1>
          <p className="hint">{t('intro')}</p>
        </div>
      </header>
      <div className="segmented" role="group" aria-label={t('title')}>
        {(['domains', 'accounts'] as const).map((v) => (
          <button key={v} type="button" aria-pressed={view === v} onClick={() => setParams({ view: v }, { replace: true })}>
            {t(v === 'domains' ? 'domains.title' : 'accounts.title')}
          </button>
        ))}
      </div>
      {view === 'domains' ? <DomainsSection /> : providers.data && <AccountsSection providers={providers.data} />}
    </>
  )
}

import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { AccountsSection } from './AccountsSection'
import type { Provider } from './AccountsSection'
import { DomainsSection } from './DomainsSection'

export function DnsPage() {
  const { t } = useTranslation('dns')
  const providers = useQuery({ queryKey: ['dns-providers'], queryFn: () => api.get<Provider[]>('/dns/providers') })
  return (
    <>
      <h1>{t('title')}</h1>
      <p>{t('intro')}</p>
      <DomainsSection />
      {providers.data && <AccountsSection providers={providers.data} />}
    </>
  )
}

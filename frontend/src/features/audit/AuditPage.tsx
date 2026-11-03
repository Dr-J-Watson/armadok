import { useTranslation } from 'react-i18next'
import { AuditList } from './AuditList'

export function AuditPage() {
  const { t } = useTranslation('audit')
  return (
    <>
      <h1>{t('title')}</h1>
      <p>{t('intro')}</p>
      <AuditList />
    </>
  )
}

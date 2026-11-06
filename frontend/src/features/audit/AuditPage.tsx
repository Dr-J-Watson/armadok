import { useTranslation } from 'react-i18next'
import { AuditList } from './AuditList'

export function AuditPage() {
  const { t } = useTranslation('audit')
  return (
    <>
      <header className="page-head">
        <div>
          <h1>{t('title')}</h1>
          <p className="hint">{t('intro')}</p>
        </div>
      </header>
      <AuditList />
    </>
  )
}

import { useTranslation } from 'react-i18next'
import { HooksSection } from './HooksSection'

// Where outside tools connect: hooks for CI, and how to read the metrics.
export function IntegrationsPage() {
  const { t } = useTranslation(['integrations'])
  const scrape = `scrape_configs:
  - job_name: armadok
    scheme: https
    metrics_path: /api/v1/metrics
    authorization:
      credentials: <an API key with the metrics.read right>
    static_configs:
      - targets: ['${location.host}']`
  return (
    <>
      <h1>{t('integrations:title')}</h1>
      <HooksSection />
      <section>
        <h2>{t('integrations:metrics.title')}</h2>
        <p>{t('integrations:metrics.explain')}</p>
        <pre className="output">{scrape}</pre>
      </section>
    </>
  )
}

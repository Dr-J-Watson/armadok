import { Compass } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

// A page that does not exist, said kindly, with a way back.
export function NotFound() {
  const { t } = useTranslation()
  return (
    <div className="empty">
      <Compass size={28} aria-hidden="true" />
      <h2>{t('notFound')}</h2>
      <p>{t('notFoundHint')}</p>
      <Link to="/dashboard" className="button-link">
        {t('nav.dashboard')}
      </Link>
    </div>
  )
}

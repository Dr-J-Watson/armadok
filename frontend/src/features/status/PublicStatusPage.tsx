import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { api } from '../../core/api'

interface Status {
  title: string
  state: 'up' | 'down' | 'unknown'
  since: string | null
  checked_at: string | null
  availability: number | null
  recent: boolean[]
}

// The page anyone with the address may open, signed in or not: a title, up or down, and dots.
export function PublicStatusPage() {
  const { t } = useTranslation('status')
  const { slug = '' } = useParams()
  const status = useQuery({
    queryKey: ['public-status', slug],
    queryFn: () => api.get<Status>(`/status/${encodeURIComponent(slug)}`),
    refetchInterval: 30_000,
    retry: false,
  })
  if (status.isError) return <main className="public-status"><p className="empty">{t('notFound')}</p></main>
  if (!status.data) return <main className="public-status"><p className="loading">…</p></main>
  const s = status.data
  return (
    <main className="public-status">
      <h1>{s.title}</h1>
      <p>
        <span className={`badge ${s.state === 'up' ? 'ok' : s.state === 'down' ? 'danger-badge' : 'off'}`}>{t(`state.${s.state}`)}</span>
      </p>
      <p className="history" aria-label={t('recent')}>
        {s.recent.map((ok, i) => (
          <span key={i} className={ok ? 'dot ok' : 'dot bad'}>
            {ok ? '▮' : '▯'}
          </span>
        ))}
      </p>
      {s.availability !== null && <p className="hint">{t('availability', { value: s.availability })}</p>}
      {s.checked_at && <p className="hint">{t('checked', { at: new Date(s.checked_at).toLocaleString() })}</p>}
    </main>
  )
}

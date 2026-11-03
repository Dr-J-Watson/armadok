import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'

// The bell in the top bar: how many notifications are waiting. Checked every half minute.
export function Bell() {
  const { t } = useTranslation('notifications')
  const count = useQuery({
    queryKey: ['notifications', 'unread'],
    queryFn: () => api.get<{ unread: number }>('/notifications/unread-count'),
    refetchInterval: 30000,
  })
  const unread = count.data?.unread ?? 0
  return (
    <Link to="/notifications" className="bell" aria-label={t('bell', { count: unread })}>
      <span aria-hidden="true">🔔</span>
      {unread > 0 && <span className="bell-count">{unread > 99 ? '99+' : unread}</span>}
    </Link>
  )
}

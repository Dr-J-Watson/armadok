import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCheck } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface Entry {
  id: number
  event: string
  severity: 'info' | 'warning' | 'critical'
  node_id: string | null
  title: string
  body: string
  created_at: string
  read: boolean
}

interface Page {
  entries: Entry[]
  next_before_id: number | null
}

export function Inbox() {
  const { t } = useTranslation(['notifications', 'errors'])
  const queryClient = useQueryClient()
  const inbox = useInfiniteQuery({
    queryKey: ['notifications', 'inbox'],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => api.get<Page>(`/notifications${pageParam ? `?before_id=${pageParam}` : ''}`),
    getNextPageParam: (last) => last.next_before_id,
  })
  const readAll = useMutation({
    mutationFn: () => api.post('/notifications/read', { ids: null }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  })
  const readOne = useMutation({
    mutationFn: (id: number) => api.post('/notifications/read', { ids: [id] }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  })
  const entries = inbox.data?.pages.flatMap((p) => p.entries) ?? []

  return (
    <>
      {inbox.isError && <Problem error={inbox.error} />}
      {inbox.isSuccess && entries.length === 0 && <p className="empty">{t('notifications:inbox.empty')}</p>}
      {inbox.isPending && <p className="loading">{t('loading')}</p>}
      {entries.some((e) => !e.read) && (
        <div className="actions">
          <button type="button" disabled={readAll.isPending} title={t('notifications:inbox.readAllTip')} onClick={() => readAll.mutate()}>
            <CheckCheck size={14} aria-hidden="true" /> {t('notifications:inbox.readAll')}
          </button>
        </div>
      )}
      <ul className="notes-list">
        {entries.map((e) => (
          <li key={e.id} className={`note note-${e.severity === 'critical' ? 'todo' : e.severity}${e.read ? '' : ' unread'}`}>
            <div className="note-head">
              <strong>{e.title}</strong>
              <span className={`badge${e.severity === 'critical' ? ' danger-badge' : ''}`}>{t(`notifications:severity.${e.severity}`)}</span>
              <small><time dateTime={e.created_at}>{new Date(e.created_at).toLocaleString()}</time></small>
            </div>
            <p className="note-body">{e.body}</p>
            <div className="actions">
              {e.node_id && <Link to={`/nodes/${e.node_id}`}>{t('notifications:inbox.openNode')}</Link>}
              {!e.read && (
                <button type="button" className="link-button" onClick={() => readOne.mutate(e.id)}>
                  {t('notifications:inbox.markRead')}
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
      {inbox.hasNextPage && (
        <button type="button" disabled={inbox.isFetchingNextPage} onClick={() => void inbox.fetchNextPage()}>
          {t('notifications:inbox.more')}
        </button>
      )}
    </>
  )
}

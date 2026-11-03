import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

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
      {inbox.isError && <p role="alert" className="error">{errorMessage(t, inbox.error)}</p>}
      {inbox.isSuccess && entries.length === 0 && <p className="empty">{t('notifications:inbox.empty')}</p>}
      {entries.some((e) => !e.read) && (
        <button type="button" onClick={() => readAll.mutate()}>{t('notifications:inbox.readAll')}</button>
      )}
      <ul className="notes-list">
        {entries.map((e) => (
          <li key={e.id} className={`note note-${e.severity === 'critical' ? 'todo' : e.severity}${e.read ? '' : ' unread'}`}>
            <div className="note-head">
              <strong>{e.title}</strong>
              <span className="badge">{t(`notifications:severity.${e.severity}`)}</span>
              <small>{new Date(e.created_at).toLocaleString()}</small>
            </div>
            <p className="note-body">{e.body}</p>
            {!e.read && (
              <button type="button" className="link-button" onClick={() => readOne.mutate(e.id)}>
                {t('notifications:inbox.markRead')}
              </button>
            )}
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

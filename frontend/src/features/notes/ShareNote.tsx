import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Person {
  user_id: string
  username: string
}

// Who a note is shown to (read-only), and a search to add someone who can see the machine.
// A note is private to its author unless they do this on purpose.
export function ShareNote({ noteId, nodeId }: { noteId: string; nodeId: string }) {
  const { t } = useTranslation(['notes', 'errors'])
  const client = useQueryClient()
  const [search, setSearch] = useState('')
  const [asked, setAsked] = useState('')
  const key = ['note-shares', noteId]
  const shares = useQuery({ queryKey: key, queryFn: () => api.get<Person[]>(`/notes/${noteId}/shares`) })
  const candidates = useQuery({
    queryKey: ['note-people', nodeId, asked],
    enabled: asked !== '',
    retry: false,
    queryFn: () => api.get<{ id: string; username: string }[]>(`/notes/people?node_id=${encodeURIComponent(nodeId)}&username=${encodeURIComponent(asked)}`),
  })
  const refresh = () => client.invalidateQueries({ queryKey: key })
  const add = useMutation({
    mutationFn: (userId: string) => api.post(`/notes/${noteId}/shares`, { user_id: userId }),
    onSuccess: () => {
      setSearch('')
      setAsked('')
      return refresh()
    },
  })
  const remove = useMutation({ mutationFn: (userId: string) => api.del(`/notes/${noteId}/shares/${userId}`), onSuccess: refresh })
  const failed = shares.error ?? candidates.error ?? add.error ?? remove.error

  return (
    <div className="notice">
      <p>{shares.data?.length ? t('notes:sharedWith') : t('notes:notShared')}</p>
      <ul className="links">
        {shares.data?.map((p) => (
          <li key={p.user_id}>
            {p.username}{' '}
            <button type="button" className="link-button" onClick={() => remove.mutate(p.user_id)}>
              {t('notes:unshare')}
            </button>
          </li>
        ))}
      </ul>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          setAsked(search.trim())
        }}
      >
        <label>
          {t('notes:shareWith')}
          <input value={search} maxLength={64} onChange={(e) => setSearch(e.target.value)} />
        </label>{' '}
        <button type="submit" disabled={!search.trim()}>
          {t('notes:find')}
        </button>
      </form>
      {candidates.data?.length === 0 && asked !== '' && <p className="hint">{t('notes:nobody')}</p>}
      <ul className="links">
        {candidates.data?.map((p) => (
          <li key={p.id}>
            <button type="button" className="link-button" onClick={() => add.mutate(p.id)}>
              {p.username}
            </button>
          </li>
        ))}
      </ul>
      <p className="hint">{t('notes:shareHint')}</p>
      {failed != null && <p role="alert" className="error">{errorMessage(t, failed)}</p>}
    </div>
  )
}

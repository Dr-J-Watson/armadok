import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { NoteItem } from './NotesPanel'
import type { Note } from './NotesPanel'

// All the caller's notes, with search and a filter on open reminders.
export function NotesPage() {
  const { t } = useTranslation(['notes', 'errors'])
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState(false)
  const params = new URLSearchParams()
  if (search.trim()) params.set('q', search.trim())
  if (open) params.set('done', 'false')
  const notes = useQuery({ queryKey: ['notes', 'all', params.toString()], queryFn: () => api.get<Note[]>(`/notes?${params}`) })

  return (
    <>
      <h1>{t('notes:page')}</h1>
      <p className="hint">{t('notes:private')}</p>
      <div className="filters">
        <label>
          <span className="visually-hidden">{t('notes:search')}</span>
          <input type="search" placeholder={t('notes:search')} value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <label className="legend-item">
          <input type="checkbox" checked={open} onChange={(e) => setOpen(e.target.checked)} />
          {t('notes:onlyOpen')}
        </label>
      </div>
      {notes.isError && <p role="alert" className="error">{errorMessage(t, notes.error)}</p>}
      {notes.data?.length === 0 && <p className="empty">{t('notes:empty')}</p>}
      <ul className="notes-list">
        {notes.data?.map((n) => (
          <li key={n.id} className="note-wrap">
            <p className="hint">
              <Link to={`/nodes/${n.node_id}?tab=map&focus=${encodeURIComponent(`${n.object_type}:${n.object_id}`)}`}>
                {n.object_type} · {n.object_id}
              </Link>
            </p>
            <ul className="notes-list">
              <NoteItem note={n} onChanged={() => void queryClient.invalidateQueries({ queryKey: ['notes'] })} />
            </ul>
          </li>
        ))}
      </ul>
    </>
  )
}

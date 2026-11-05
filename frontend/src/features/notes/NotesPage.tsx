import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { NoteItem } from './NotesPanel'
import type { Note } from './NotesPanel'
import { Problem } from '../../components/Problem'

// All the caller's notes, with search and a filter on open reminders.
export function NotesPage() {
  const { t } = useTranslation(['notes', 'errors'])
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState(false)
  const params = new URLSearchParams()
  if (open) params.set('done', 'false')
  const all = useQuery({ queryKey: ['notes', 'all', params.toString()], queryFn: () => api.get<Note[]>(`/notes?${params}`) })
  // What is typed is a piece of a private note: it stays in the browser, out of every address.
  const needle = search.trim().toLowerCase()
  const notes = {
    ...all,
    data: all.data?.filter((n) => !needle || n.body.toLowerCase().includes(needle) || n.object_id.toLowerCase().includes(needle)),
  }

  const shared = useQuery({ queryKey: ['notes', 'shared'], queryFn: () => api.get<Note[]>('/notes/shared') })

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
      {shared.data && shared.data.length > 0 && (
        <>
          <h2>{t('notes:sharedWithMe')}</h2>
          <ul className="notes-list">
            {shared.data.map((n) => (
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
          <h2>{t('notes:mine')}</h2>
        </>
      )}
      {notes.isError && <Problem error={notes.error} />}
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

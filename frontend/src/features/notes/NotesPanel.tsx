import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { ShareNote } from './ShareNote'

export interface Note {
  id: string
  node_id: string
  object_type: string
  object_id: string
  body: string
  importance: 'info' | 'warning' | 'todo'
  pinned: boolean
  done: boolean
  remind_at: string | null
  updated_at: string
  author?: string
  read_only?: boolean
}

const IMPORTANCE = ['info', 'warning', 'todo'] as const

// One note: shown, and editable in place. Text only (no HTML): a note can never inject markup.
export function NoteItem({ note, onChanged }: { note: Note; onChanged: () => void }) {
  const { t } = useTranslation(['notes', 'errors'])
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(note.body)
  const [sharing, setSharing] = useState(false)
  const patch = useMutation({
    mutationFn: (changes: Record<string, unknown>) =>
      api.patch(`/notes/${note.id}`, { ...changes, expected_updated_at: note.updated_at }),
    onSuccess: () => {
      setEditing(false)
      onChanged()
    },
  })
  const remove = useMutation({ mutationFn: () => api.del(`/notes/${note.id}`), onSuccess: onChanged })
  const conflict = patch.error instanceof ApiError && patch.error.code === 'notes.conflict'

  return (
    <li className={`note note-${note.importance}${note.done ? ' note-done' : ''}`}>
      <div className="note-head">
        <label className="legend-item">
          <input
            type="checkbox"
            checked={note.done}
            disabled={note.read_only}
            onChange={(e) => patch.mutate({ done: e.target.checked })}
          />
          <span className="visually-hidden">{t('notes:done')}</span>
        </label>
        <span className="badge">{t(`notes:importance.${note.importance}`)}</span>
        {note.pinned && <span className="badge">{t('notes:pinned')}</span>}
        {note.remind_at && <small>{t('notes:remindOn', { date: new Date(note.remind_at).toLocaleString() })}</small>}
      </div>
      {editing ? (
        <>
          <label htmlFor={`note-${note.id}`} className="visually-hidden">
            {t('notes:edit')}
          </label>
          <textarea id={`note-${note.id}`} className="note-text" maxLength={4000} value={text} onChange={(e) => setText(e.target.value)} />
          {conflict && <p role="alert" className="error">{t('errors:notes.conflict')}</p>}
          {patch.isError && !conflict && <p role="alert" className="error">{errorMessage(t, patch.error)}</p>}
          <button type="button" className="primary" disabled={!text.trim()} onClick={() => patch.mutate({ body: text })}>
            {t('notes:save')}
          </button>{' '}
          <button type="button" onClick={() => { setEditing(false); setText(note.body) }}>{t('notes:cancel')}</button>
        </>
      ) : (
        <p className="note-body">{note.body}</p>
      )}
      {note.read_only && <p className="hint">{t('notes:from', { author: note.author })}</p>}
      {sharing && !note.read_only && <ShareNote noteId={note.id} nodeId={note.node_id} />}
      {!editing && !note.read_only && (
        <div className="note-actions">
          <button type="button" className="link-button" onClick={() => setSharing(!sharing)}>{t('notes:share')}</button>
          <button type="button" className="link-button" onClick={() => setEditing(true)}>{t('notes:edit')}</button>
          <button type="button" className="link-button" onClick={() => patch.mutate({ pinned: !note.pinned })}>
            {note.pinned ? t('notes:unpin') : t('notes:pin')}
          </button>
          <button type="button" className="link-button" onClick={() => remove.mutate()}>{t('notes:delete')}</button>
        </div>
      )}
    </li>
  )
}

interface Props {
  nodeId: string
  objectType: string
  objectId: string
}

// The caller's own notes on one object, plus a form to add one. Private to the author: the
// server returns nothing but their own notes.
export function NotesPanel({ nodeId, objectType, objectId }: Props) {
  const { t } = useTranslation(['notes', 'errors'])
  const queryClient = useQueryClient()
  const key = ['notes', nodeId, objectType, objectId]
  const path = `/notes?node_id=${encodeURIComponent(nodeId)}&object_type=${encodeURIComponent(objectType)}&object_id=${encodeURIComponent(objectId)}`
  const notes = useQuery({ queryKey: key, queryFn: () => api.get<Note[]>(path) })
  const [body, setBody] = useState('')
  const [importance, setImportance] = useState<Note['importance']>('info')
  const [remind, setRemind] = useState('')

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['notes'] })
  const create = useMutation({
    mutationFn: () =>
      api.post('/notes', {
        node_id: nodeId,
        object_type: objectType,
        object_id: objectId,
        body,
        importance,
        remind_at: remind ? new Date(remind).toISOString() : null,
      }),
    onSuccess: () => {
      setBody('')
      setRemind('')
      return refresh()
    },
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  return (
    <section className="notes">
      <h3>{t('notes:title')}</h3>
      <p className="hint">{t('notes:private')}</p>
      {notes.isError && <p role="alert" className="error">{errorMessage(t, notes.error)}</p>}
      <ul className="notes-list">
        {notes.data?.map((n) => <NoteItem key={`${n.id}:${n.updated_at}`} note={n} onChanged={() => void refresh()} />)}
      </ul>
      <form className="form" onSubmit={submit}>
        <label htmlFor={`new-note-${objectId}`}>{t('notes:new')}</label>
        <textarea id={`new-note-${objectId}`} className="note-text" required maxLength={4000} value={body} onChange={(e) => setBody(e.target.value)} />
        <label htmlFor={`note-importance-${objectId}`}>{t('notes:importanceLabel')}</label>
        <select id={`note-importance-${objectId}`} value={importance} onChange={(e) => setImportance(e.target.value as Note['importance'])}>
          {IMPORTANCE.map((i) => <option key={i} value={i}>{t(`notes:importance.${i}`)}</option>)}
        </select>
        <label htmlFor={`note-remind-${objectId}`}>{t('notes:remind')}</label>
        <input id={`note-remind-${objectId}`} type="datetime-local" value={remind} onChange={(e) => setRemind(e.target.value)} />
        {create.isError && <p role="alert" className="error">{errorMessage(t, create.error)}</p>}
        <button type="submit" className="primary" disabled={create.isPending || !body.trim()}>{t('notes:add')}</button>
      </form>
    </section>
  )
}

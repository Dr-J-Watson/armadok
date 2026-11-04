import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Item {
  type: string
  id: string
}

interface Owners {
  types: string[]
  node_owner: boolean
  can_take: boolean
  mine: Item[]
  others: (Item & { owner: string; owner_name: string | null })[]
}

interface Person {
  id: string
  username: string
}

// Who owns an object, and the ways to hand it over or take it back. Shown under every object
// of a kind that can have its own owner; the server decides what is allowed.
export function OwnershipSection({ nodeId, objectType, objectId }: { nodeId: string; objectType: string; objectId: string }) {
  const { t } = useTranslation(['ownership', 'errors'])
  const client = useQueryClient()
  const key = ['ownership', 'node', nodeId]
  const owners = useQuery({ queryKey: key, queryFn: () => api.get<Owners>(`/nodes/${nodeId}/ownership`) })
  const [cascade, setCascade] = useState(false)
  if (!owners.data || !owners.data.types.includes(objectType)) return null
  const data = owners.data
  const here = (i: Item) => i.type === objectType && i.id === objectId
  const other = data.others.find(here)
  // Mine by a row, or by being the machine's owner of an object nobody else owns.
  const mine = data.mine.some(here) || (data.node_owner && !other)
  if (!mine && !other) return null
  const done = () => client.invalidateQueries({ queryKey: ['ownership'] })

  return (
    <section>
      <h3>{t('ownership:title')}</h3>
      {other && <p>{t('ownership:ownedBy', { name: other.owner_name ?? '?' })}</p>}
      {mine && <p>{data.node_owner ? t('ownership:mineDefault') : t('ownership:mine')}</p>}
      {mine && <GiveForm nodeId={nodeId} target={cascade ? null : { type: objectType, id: objectId }} only={objectType} onDone={done} />}
      {(mine || other) && (
        <label className="legend-item">
          <input type="checkbox" checked={cascade} onChange={(e) => setCascade(e.target.checked)} />
          {t('ownership:cascade')}
        </label>
      )}
      {other && data.can_take && <TakeForm nodeId={nodeId} target={cascade ? null : { type: objectType, id: objectId }} only={objectType} onDone={done} />}
      {cascade && data.can_take && !other && <TakeForm nodeId={nodeId} target={null} only={objectType} onDone={done} />}
    </section>
  )
}

function Preview({ nodeId, mode, only }: { nodeId: string; mode: 'give' | 'take'; only: string }) {
  const { t } = useTranslation(['ownership', 'errors'])
  const list = useQuery({
    queryKey: ['ownership', 'cascade', nodeId, mode, only],
    queryFn: () => api.get<{ items: Item[] }>(`/nodes/${nodeId}/ownership/cascade?mode=${mode}&only_type=${encodeURIComponent(only)}`),
  })
  if (list.isError) return <p role="alert" className="error">{errorMessage(t, list.error)}</p>
  return (
    <div>
      <p className="hint">{t('ownership:willMove', { count: list.data?.items.length ?? 0 })}</p>
      <ul className="links">
        {list.data?.items.map((i) => (
          <li key={`${i.type}:${i.id}`}>
            {i.type} · {i.id}
          </li>
        ))}
      </ul>
    </div>
  )
}

// Give: find the person (exact user name), choose to hand the notes too, then confirm.
function GiveForm({ nodeId, target, only, onDone }: { nodeId: string; target: Item | null; only: string; onDone: () => void }) {
  const { t } = useTranslation(['ownership', 'errors'])
  const [name, setName] = useState('')
  const [asked, setAsked] = useState('')
  const [notes, setNotes] = useState(false)
  const [code, setCode] = useState('')
  const [sent, setSent] = useState(false)
  const found = useQuery({
    queryKey: ['ownership', 'people', nodeId, asked],
    enabled: asked !== '',
    retry: false,
    queryFn: () => api.get<Person[]>(`/nodes/${nodeId}/ownership/people?username=${encodeURIComponent(asked)}`),
  })
  const person = found.data?.[0]
  const give = useMutation({
    mutationFn: () =>
      api.post(`/nodes/${nodeId}/ownership/offers`, {
        to_user: person?.id,
        with_notes: notes,
        code,
        ...(target ? { object_type: target.type, object_id: target.id } : { cascade: true, only_type: only }),
      }),
    onSuccess: () => {
      setSent(true)
      setCode('')
      onDone()
    },
  })
  const search = (e: FormEvent) => {
    e.preventDefault()
    setSent(false)
    setAsked(name.trim())
  }
  return (
    <div className="notice">
      <h4>{t('ownership:give')}</h4>
      {!target && <Preview nodeId={nodeId} mode="give" only={only} />}
      <form onSubmit={search}>
        <label>
          {t('ownership:recipient')}
          <input value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
        </label>{' '}
        <button type="submit">{t('ownership:find')}</button>
      </form>
      {asked !== '' && found.data && !person && <p className="hint">{t('ownership:nobody')}</p>}
      {person && (
        <>
          <p>{t('ownership:giveTo', { name: person.username })}</p>
          <label className="legend-item">
            <input type="checkbox" checked={notes} onChange={(e) => setNotes(e.target.checked)} />
            {t('ownership:withNotes')}
          </label>
          <label>
            {t('ownership:code')}
            <input value={code} inputMode="numeric" autoComplete="one-time-code" maxLength={30} onChange={(e) => setCode(e.target.value)} />
          </label>{' '}
          <button type="button" disabled={give.isPending} onClick={() => give.mutate()}>
            {t('ownership:send')}
          </button>
        </>
      )}
      {sent && <p role="status">{t('ownership:giftSent')}</p>}
      {(found.error ?? give.error) && <p role="alert" className="error">{errorMessage(t, found.error ?? give.error)}</p>}
    </div>
  )
}

// Take back: only the machine's owner sees this. Notes are never taken.
function TakeForm({ nodeId, target, only, onDone }: { nodeId: string; target: Item | null; only: string; onDone: () => void }) {
  const { t } = useTranslation(['ownership', 'errors'])
  const [code, setCode] = useState('')
  const take = useMutation({
    mutationFn: () =>
      api.post(`/nodes/${nodeId}/ownership/take`, {
        code,
        ...(target ? { object_type: target.type, object_id: target.id } : { cascade: true, only_type: only }),
      }),
    onSuccess: () => {
      setCode('')
      onDone()
    },
  })
  return (
    <div className="notice">
      <h4>{t('ownership:take')}</h4>
      {!target && <Preview nodeId={nodeId} mode="take" only={only} />}
      <p className="hint">{t('ownership:takeHint')}</p>
      <label>
        {t('ownership:code')}
        <input value={code} inputMode="numeric" autoComplete="one-time-code" maxLength={30} onChange={(e) => setCode(e.target.value)} />
      </label>{' '}
      <button type="button" disabled={take.isPending} onClick={() => take.mutate()}>
        {t('ownership:takeNow')}
      </button>
      {take.isError && <p role="alert" className="error">{errorMessage(t, take.error)}</p>}
    </div>
  )
}

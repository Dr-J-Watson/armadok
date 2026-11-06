import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import type { Channel } from './Channels'
import { Problem } from '../../components/Problem'

interface EventInfo {
  name: string
  severity: string
  title: string
}

interface Rule {
  event: string
  target: string
  min_severity: string
  node_id: string | null
  enabled: boolean
}

interface Choice {
  targets: Set<string>
  min: string
}

// Per event: which places get it and from which severity. An event with no rule goes to the
// inbox. Rules limited to one node (made through the API) are kept untouched when saving.
export function Rules() {
  const { t } = useTranslation(['notifications', 'errors'])
  const queryClient = useQueryClient()
  const events = useQuery({ queryKey: ['notif-events'], queryFn: () => api.get<EventInfo[]>('/notifications/events') })
  const channels = useQuery({ queryKey: ['channels'], queryFn: () => api.get<Channel[]>('/notifications/channels') })
  const rules = useQuery({ queryKey: ['notif-rules'], queryFn: () => api.get<Rule[]>('/notifications/rules') })
  // What the server has, turned into choices; the user's edits are layered on top.
  const saved = useMemo(() => {
    const result: Record<string, Choice> = {}
    for (const e of events.data ?? []) {
      const mine = (rules.data ?? []).filter((r) => r.event === e.name && r.node_id === null)
      result[e.name] = mine.length
        ? { targets: new Set(mine.filter((r) => r.enabled).map((r) => r.target)), min: mine[0].min_severity }
        : { targets: new Set(['in_app']), min: 'info' }
    }
    return result
  }, [events.data, rules.data])
  const [edits, setEdits] = useState<Record<string, Choice>>({})
  const choices = events.data && rules.data ? { ...saved, ...edits } : null
  const setChoices = (next: Record<string, Choice>) => setEdits(next)

  const save = useMutation({
    mutationFn: () => {
      const kept = (rules.data ?? []).filter((r) => r.node_id !== null)
      const managed = Object.entries(choices ?? {}).flatMap(([event, c]) =>
        [...c.targets].map((target) => ({ event, target, min_severity: c.min, node_id: null, enabled: true })),
      )
      return api.put('/notifications/rules', { rules: [...managed, ...kept] })
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['notif-rules'] })
      setEdits({})
    },
  })

  if (!choices || !events.data) return <p className="loading">{t('loading')}</p>
  const places = [{ id: 'in_app', name: t('notifications:rules.inApp') }, ...(channels.data ?? []).map((c) => ({ id: c.id, name: c.name }))]

  function toggle(event: string, place: string) {
    const current = choices![event]
    const targets = new Set(current.targets)
    if (targets.has(place)) targets.delete(place)
    else targets.add(place)
    setChoices({ ...choices, [event]: { ...current, targets } })
  }

  return (
    <>
      <p className="hint">{t('notifications:rules.explain')}</p>
      {(save.error ?? rules.error) != null && <Problem error={save.error ?? rules.error} />}
      <table className="table">
        <thead>
          <tr>
            <th>{t('notifications:rules.event')}</th>
            <th>{t('notifications:rules.minSeverity')}</th>
            <th>{t('notifications:rules.where')}</th>
          </tr>
        </thead>
        <tbody>
          {events.data.map((e) => (
            <tr key={e.name}>
              <td data-label={t('notifications:rules.event')}>{e.title}</td>
              <td data-label={t('notifications:rules.minSeverity')}>
                <select
                  aria-label={`${e.title} — ${t('notifications:rules.minSeverity')}`}
                  value={choices[e.name].min}
                  onChange={(ev) => setChoices({ ...choices, [e.name]: { ...choices[e.name], min: ev.target.value } })}
                >
                  {['info', 'warning', 'critical'].map((s) => <option key={s} value={s}>{t(`notifications:severity.${s}`)}</option>)}
                </select>
              </td>
              <td data-label={t('notifications:rules.where')}>
                {places.map((p) => (
                  <label key={p.id} className="legend-item">
                    <input type="checkbox" checked={choices[e.name].targets.has(p.id)} onChange={() => toggle(e.name, p.id)} />
                    {p.name}
                  </label>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="actions">
        <button type="button" className="primary" disabled={save.isPending || Object.keys(edits).length === 0} onClick={() => save.mutate()}>
          {t('notifications:rules.save')}
        </button>
        {Object.keys(edits).length > 0 && <span className="hint">{t('notifications:rules.unsaved')}</span>}
      </div>
      {save.isSuccess && <p role="status" className="ok-text">{t('notifications:rules.saved')}</p>}
    </>
  )
}

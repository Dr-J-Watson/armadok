import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Field {
  name: string
  choices: string[]
}

interface Action {
  name: string
  fields: Field[]
}

type Schedule =
  | { kind: 'interval'; every_minutes: number }
  | { kind: 'daily'; at: string; timezone: string; weekdays: number[] }

interface Task {
  id: string
  name: string
  action: string
  target: Record<string, string>
  schedule: Schedule
  enabled: boolean
  next_run: string | null
  last_run: string | null
  last_result: string | null
}

interface Answer {
  tasks: Task[]
  actions: Action[]
  can_manage: boolean
}

const DAYS = [0, 1, 2, 3, 4, 5, 6]
const EMPTY = { name: '', action: '', target: {} as Record<string, string>, kind: 'daily', every: '1440', at: '03:00', weekdays: [] as number[] }
const zone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

// Actions the machine does by itself, on a schedule, with the rights of the person who set
// them up: they are checked again at every run.
export function ScheduleTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['schedule', 'errors'])
  const client = useQueryClient()
  const [form, setForm] = useState(EMPTY)
  const list = useQuery({
    queryKey: ['schedule', nodeId],
    queryFn: () => api.get<Answer>(`/nodes/${nodeId}/schedule`),
  })
  const refresh = () => client.invalidateQueries({ queryKey: ['schedule', nodeId] })
  const base = `/nodes/${nodeId}/schedule`
  const action = list.data?.actions.find((a) => a.name === form.action)
  const create = useMutation({
    mutationFn: () =>
      api.post(base, {
        name: form.name,
        action: form.action,
        target: form.target,
        schedule:
          form.kind === 'interval'
            ? { kind: 'interval', every_minutes: Number(form.every) }
            : { kind: 'daily', at: form.at, timezone: zone(), weekdays: form.weekdays },
      }),
    onSuccess: () => {
      setForm(EMPTY)
      refresh()
    },
  })
  const toggle = useMutation({ mutationFn: (x: Task) => api.patch(`${base}/${x.id}`, { enabled: !x.enabled }), onSuccess: refresh })
  const remove = useMutation({ mutationFn: (x: Task) => api.del(`${base}/${x.id}`), onSuccess: refresh })
  const run = useMutation({ mutationFn: (x: Task) => api.post<{ result: string }>(`${base}/${x.id}/run`), onSuccess: refresh })

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }
  const failed = list.error ?? create.error ?? toggle.error ?? remove.error ?? run.error
  const when = (s: Schedule) =>
    s.kind === 'interval'
      ? t('schedule:when.interval', { count: s.every_minutes })
      : t('schedule:when.daily', {
          at: s.at,
          zone: s.timezone,
          days: s.weekdays.length ? s.weekdays.map((d) => t(`schedule:days.${d}`)).join(', ') : t('schedule:days.all'),
        })
  const target = (x: Task) =>
    Object.entries(x.target)
      .filter(([k]) => k !== 'node_id')
      .map(([k, v]) => t(`schedule:choices.${k}.${v}`, { defaultValue: v }))
      .join(' · ')

  return (
    <>
      <p>{t('schedule:explain')}</p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      {run.data && (
        <p role="status" className="notice">
          {run.data.result === 'ok' ? t('schedule:ran') : t('schedule:ranFailed', { code: run.data.result })}
        </p>
      )}
      {list.isPending && <p>{t('loading')}</p>}
      {list.data && list.data.tasks.length === 0 && <p className="empty">{t('schedule:none')}</p>}
      <ul className="links">
        {(list.data?.tasks ?? []).map((x) => (
          <li key={x.id}>
            <strong>{x.name}</strong> <span className={`badge ${x.enabled ? 'ok' : 'off'}`}>{t(x.enabled ? 'schedule:on' : 'schedule:off')}</span>{' '}
            {t(`schedule:actions.${x.action}`, { defaultValue: x.action })} {target(x)} — {when(x.schedule)}
            {x.next_run && <span> — {t('schedule:next', { at: new Date(x.next_run).toLocaleString() })}</span>}
            {x.last_result && (
              <span className={x.last_result === 'ok' ? '' : 'error'}>
                {' '}
                — {t('schedule:last', { at: x.last_run ? new Date(x.last_run).toLocaleString() : '' })}{' '}
                {x.last_result === 'ok' ? t('schedule:ok') : t(`errors:${x.last_result}`, { defaultValue: x.last_result })}
              </span>
            )}
            {list.data?.can_manage && (
              <span>
                {' '}
                <button type="button" onClick={() => run.mutate(x)}>
                  {t('schedule:runNow')}
                </button>{' '}
                <button type="button" onClick={() => toggle.mutate(x)}>
                  {t(x.enabled ? 'schedule:pause' : 'schedule:resume')}
                </button>{' '}
                <button type="button" onClick={() => window.confirm(t('schedule:confirmDelete')) && remove.mutate(x)}>
                  {t('schedule:delete')}
                </button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {list.data?.can_manage && (
        <form onSubmit={submit} aria-label={t('schedule:new')}>
          <h2>{t('schedule:new')}</h2>
          <label>
            {t('schedule:name')}
            <input value={form.name} required maxLength={60} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label>
            {t('schedule:action')}
            <select value={form.action} required onChange={(e) => setForm({ ...form, action: e.target.value, target: {} })}>
              <option value="" />
              {list.data.actions.map((a) => (
                <option key={a.name} value={a.name}>
                  {t(`schedule:actions.${a.name}`, { defaultValue: a.name })}
                </option>
              ))}
            </select>
          </label>
          {(action?.fields ?? []).map((f) => (
            <label key={f.name}>
              {t(`schedule:fields.${f.name}`, { defaultValue: f.name })}
              {f.choices.length > 0 ? (
                <select value={form.target[f.name] ?? ''} required onChange={(e) => setForm({ ...form, target: { ...form.target, [f.name]: e.target.value } })}>
                  <option value="" />
                  {f.choices.map((c) => (
                    <option key={c} value={c}>
                      {t(`schedule:choices.${f.name}.${c}`, { defaultValue: c })}
                    </option>
                  ))}
                </select>
              ) : (
                <input value={form.target[f.name] ?? ''} required onChange={(e) => setForm({ ...form, target: { ...form.target, [f.name]: e.target.value } })} />
              )}
            </label>
          ))}
          <label>
            {t('schedule:kind')}
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
              <option value="daily">{t('schedule:kinds.daily')}</option>
              <option value="interval">{t('schedule:kinds.interval')}</option>
            </select>
          </label>
          {form.kind === 'interval' ? (
            <label>
              {t('schedule:every')}
              <input type="number" min={15} max={10080} value={form.every} required onChange={(e) => setForm({ ...form, every: e.target.value })} />
            </label>
          ) : (
            <>
              <label>
                {t('schedule:at', { zone: zone() })}
                <input type="time" value={form.at} required onChange={(e) => setForm({ ...form, at: e.target.value })} />
              </label>
              <fieldset>
                <legend>{t('schedule:weekdays')}</legend>
                {DAYS.map((d) => (
                  <label key={d}>
                    <input
                      type="checkbox"
                      checked={form.weekdays.includes(d)}
                      onChange={(e) => setForm({ ...form, weekdays: e.target.checked ? [...form.weekdays, d] : form.weekdays.filter((x) => x !== d) })}
                    />
                    {t(`schedule:days.${d}`)}
                  </label>
                ))}
                <p className="hint">{t('schedule:everyDayHint')}</p>
              </fieldset>
            </>
          )}
          <p className="hint">{t('schedule:warn')}</p>
          <button type="submit" disabled={create.isPending}>
            {t('schedule:create')}
          </button>
        </form>
      )}
    </>
  )
}

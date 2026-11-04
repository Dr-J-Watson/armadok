import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { PublishControl } from '../status/PublishControl'

interface Result {
  at: string
  ok: boolean
  latency_ms: number | null
  reason: string | null
}

interface Probe {
  id: string
  name: string
  kind: 'http' | 'tcp'
  scheme: string | null
  host: string
  port: number
  path: string
  enabled: boolean
  state: 'unknown' | 'up' | 'down'
  interval_seconds: number
  last_checked: string | null
  last_reason: string | null
  availability: number | null
  recent: Result[]
}

interface Answer {
  probes: Probe[]
  min_interval: number
  max_interval: number
  can_manage: boolean
}

const EMPTY = { name: '', kind: 'http', url: '', host: '', port: '', interval: '60' }

// What the machine can see answering (or not) at the addresses you chose, with a short history.
export function UptimeTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['uptime', 'errors'])
  const client = useQueryClient()
  const [form, setForm] = useState(EMPTY)
  const list = useQuery({
    queryKey: ['uptime', nodeId],
    queryFn: () => api.get<Answer>(`/nodes/${nodeId}/uptime`),
    // The checks run on their own; the list follows them.
    refetchInterval: 30_000,
  })
  const refresh = () => client.invalidateQueries({ queryKey: ['uptime', nodeId] })
  const base = `/nodes/${nodeId}/uptime`
  const create = useMutation({
    mutationFn: () =>
      api.post(
        base,
        form.kind === 'http'
          ? { name: form.name, kind: 'http', url: form.url, interval_seconds: Number(form.interval) }
          : { name: form.name, kind: 'tcp', host: form.host, port: Number(form.port), interval_seconds: Number(form.interval) },
      ),
    onSuccess: () => {
      setForm(EMPTY)
      refresh()
    },
  })
  const pause = useMutation({
    mutationFn: (p: Probe) => api.patch(`${base}/${p.id}`, { enabled: !p.enabled }),
    onSuccess: refresh,
  })
  const remove = useMutation({ mutationFn: (p: Probe) => api.del(`${base}/${p.id}`), onSuccess: refresh })
  const check = useMutation({ mutationFn: (p: Probe) => api.post(`${base}/${p.id}/check`), onSuccess: refresh })

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }
  const manage = list.data?.can_manage ?? false
  const failed = list.error ?? create.error ?? pause.error ?? remove.error ?? check.error
  const target = (p: Probe) => (p.kind === 'http' ? `${p.scheme}://${p.host}:${p.port}${p.path}` : `${p.host}:${p.port}`)

  return (
    <>
      <p>{t('uptime:explain')}</p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      {list.isPending && <p>{t('loading')}</p>}
      {list.data && list.data.probes.length === 0 && <p className="empty">{t('uptime:none')}</p>}
      <ul className="links">
        {(list.data?.probes ?? []).map((p) => (
          <li key={p.id}>
            <strong>{p.name}</strong>{' '}
            <span className={`badge ${p.state === 'up' ? 'ok' : p.state === 'down' ? 'danger-badge' : 'off'}`}>
              {t(`uptime:state.${p.enabled ? p.state : 'paused'}`)}
            </span>{' '}
            <code>{target(p)}</code>
            {p.state === 'down' && p.last_reason && <span> — {t(`uptime:reason.${p.last_reason}`, { defaultValue: p.last_reason })}</span>}
            {p.availability !== null && <span> — {t('uptime:availability', { value: p.availability })}</span>}
            {/* Newest on the right: one dot per check, so a glance shows the recent past. */}
            <span className="history" aria-label={t('uptime:history')}>
              {[...p.recent].reverse().map((r) => (
                <span key={r.at} title={`${r.at}${r.latency_ms !== null ? ` · ${r.latency_ms} ms` : ''}`} className={r.ok ? 'dot ok' : 'dot bad'}>
                  {r.ok ? '▮' : '▯'}
                </span>
              ))}
            </span>
            {manage && <PublishControl nodeId={nodeId} probeId={p.id} defaultTitle={p.name} />}
            {manage && (
              <span>
                {' '}
                <button type="button" onClick={() => check.mutate(p)}>
                  {t('uptime:checkNow')}
                </button>{' '}
                <button type="button" onClick={() => pause.mutate(p)}>
                  {t(p.enabled ? 'uptime:pause' : 'uptime:resume')}
                </button>{' '}
                <button type="button" onClick={() => remove.mutate(p)}>
                  {t('uptime:delete')}
                </button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {manage && (
        <form onSubmit={submit} aria-label={t('uptime:new')}>
          <h2>{t('uptime:new')}</h2>
          <label>
            {t('uptime:name')}
            <input value={form.name} required maxLength={60} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label>
            {t('uptime:kind')}
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
              <option value="http">HTTP(S)</option>
              <option value="tcp">TCP</option>
            </select>
          </label>
          {form.kind === 'http' ? (
            <label>
              {t('uptime:url')}
              <input type="url" value={form.url} required placeholder="https://www.example.com/" onChange={(e) => setForm({ ...form, url: e.target.value })} />
            </label>
          ) : (
            <>
              <label>
                {t('uptime:host')}
                <input value={form.host} required onChange={(e) => setForm({ ...form, host: e.target.value })} />
              </label>
              <label>
                {t('uptime:port')}
                <input type="number" min={1} max={65535} value={form.port} required onChange={(e) => setForm({ ...form, port: e.target.value })} />
              </label>
            </>
          )}
          <label>
            {t('uptime:interval')}
            <input
              type="number"
              min={list.data?.min_interval ?? 30}
              max={list.data?.max_interval ?? 3600}
              value={form.interval}
              onChange={(e) => setForm({ ...form, interval: e.target.value })}
            />
          </label>
          <p className="hint">{t('uptime:warn')}</p>
          <button type="submit" disabled={create.isPending}>
            {t('uptime:create')}
          </button>
        </form>
      )}
    </>
  )
}

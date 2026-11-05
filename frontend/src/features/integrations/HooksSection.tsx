import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import type { Node } from '../nodes/types'
import { Problem } from '../../components/Problem'

interface Hook {
  id: string
  name: string
  action: string
  target: { node_id: string; project: string }
  expires_at: string | null
  revoked: boolean
  last_used_at: string | null
  last_result: string | null
}

interface Created {
  id: string
  token: string
  fire_path: string
}

// Incoming hooks: a URL and a token that do one thing (redeploy one project), for a CI tool
// that must not hold a full key. The token is shown once, here, right after creation.
export function HooksSection() {
  const { t } = useTranslation(['integrations', 'errors'])
  const queryClient = useQueryClient()
  const hooks = useQuery({
    queryKey: ['hooks'],
    queryFn: () => api.get<{ hooks: Hook[]; actions: string[] }>('/hooks'),
  })
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const [form, setForm] = useState({ name: '', node: '', project: '', expires: '' })
  const [created, setCreated] = useState<Created | null>(null)

  const projects = useQuery({
    queryKey: ['projects', form.node],
    enabled: form.node !== '',
    queryFn: () => api.get<{ projects: { name: string }[] }>(`/nodes/${form.node}/projects`),
    retry: false,
  })
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['hooks'] })
  const create = useMutation({
    mutationFn: () =>
      api.post<Created>('/hooks', {
        name: form.name,
        action: 'project.redeploy',
        target: { node_id: form.node, project: form.project },
        expires_at: form.expires ? new Date(form.expires).toISOString() : null,
      }),
    onSuccess: (made) => {
      setCreated(made)
      setForm({ name: '', node: '', project: '', expires: '' })
      return refresh()
    },
  })
  const revoke = useMutation({ mutationFn: (id: string) => api.del(`/hooks/${id}`), onSuccess: refresh })

  const nodeName = (id: string) => nodes.data?.find((n) => n.id === id)?.name ?? id
  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }
  const failed = hooks.error ?? create.error ?? revoke.error
  const url = created ? `${location.origin}${created.fire_path}` : ''

  return (
    <section>
      <h2>{t('integrations:hooks.title')}</h2>
      <p>{t('integrations:hooks.explain')}</p>
      {failed != null && (
        <Problem error={failed} />
      )}
      {created && (
        <div role="status" className="notice">
          <p>
            <strong>{t('integrations:hooks.once')}</strong>
          </p>
          <label>
            {t('integrations:hooks.token')}
            <input readOnly value={created.token} onFocus={(e) => e.currentTarget.select()} />
          </label>
          <pre className="output">{`curl -X POST -H "Authorization: Bearer ${created.token}" ${url}`}</pre>
          <button type="button" onClick={() => setCreated(null)}>
            {t('integrations:hooks.copied')}
          </button>
        </div>
      )}
      <form onSubmit={submit} aria-label={t('integrations:hooks.new')}>
        <label>
          {t('integrations:hooks.name')}
          <input value={form.name} required maxLength={60} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label>
          {t('integrations:hooks.node')}
          <select value={form.node} required onChange={(e) => setForm({ ...form, node: e.target.value, project: '' })}>
            <option value="" />
            {(nodes.data ?? []).map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('integrations:hooks.project')}
          <select value={form.project} required onChange={(e) => setForm({ ...form, project: e.target.value })}>
            <option value="" />
            {(projects.data?.projects ?? []).map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('integrations:hooks.expires')}
          <input type="datetime-local" value={form.expires} onChange={(e) => setForm({ ...form, expires: e.target.value })} />
        </label>
        <button type="submit" disabled={create.isPending}>
          {t('integrations:hooks.create')}
        </button>
      </form>
      {hooks.data && hooks.data.hooks.length === 0 && <p className="empty">{t('integrations:hooks.none')}</p>}
      <ul className="links">
        {hooks.data?.hooks.map((h) => (
          <li key={h.id}>
            <strong>{h.name}</strong>{' '}
            <span className={`badge ${h.revoked ? 'off' : 'ok'}`}>
              {t(h.revoked ? 'integrations:hooks.revoked' : 'integrations:hooks.active')}
            </span>
            <br />
            <small>
              {t(`integrations:actions.${h.action}`, { defaultValue: h.action })}: {nodeName(h.target.node_id)} / {h.target.project}
              {h.last_used_at && ` — ${t('integrations:hooks.lastUsed', { when: new Date(h.last_used_at).toLocaleString() })}`}
              {h.last_result && ` (${h.last_result})`}
            </small>
            {!h.revoked && (
              <div className="actions">
                <button type="button" onClick={() => revoke.mutate(h.id)}>
                  {t('integrations:hooks.revoke')}
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

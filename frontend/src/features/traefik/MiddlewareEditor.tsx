import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import { INTENTS, blank, body } from './middlewareForm'
import type { Form, Intent, Item } from './middlewareForm'

// The form of one middleware made from an intention (create or edit). Passwords are hashed on the
// server and never shown again.
export function MiddlewareEditor({
  nodeId,
  editing,
  initial,
  onDone,
  onCancel,
}: {
  nodeId: string
  editing: Item | null
  initial: Form
  onDone: () => void
  onCancel: () => void
}) {
  const { t } = useTranslation(['traefik', 'errors'])
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/traefik/middlewares`
  const [form, setForm] = useState<Form>(initial)
  const [shown, setShown] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: () => (editing ? api.put(`${base}/${editing.name}`, { spec: body(form), expected_sha256: editing.sha256 }) : api.post(base, { spec: body(form) })),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['traefik-middlewares', nodeId] })
      await client.invalidateQueries({ queryKey: ['traefik-sites', nodeId] })
      onDone()
    },
  })
  const preview = useMutation({
    mutationFn: () => api.post<{ content: string }>(`${base}/preview`, { spec: body(form) }),
    onSuccess: (data) => setShown(data.content),
  })
  const failed = save.error ?? preview.error
  const set = (patch: Partial<Form>) => setForm({ ...form, ...patch })
  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate()
  }
  return (
    <form onSubmit={submit} aria-label={t('traefik:middlewares.form')}>
      <h3>{editing ? t('traefik:middlewares.editing', { name: editing.name }) : t('traefik:middlewares.new')}</h3>
  {failed != null && <Problem error={failed} />}
      <label>
        {t('traefik:middlewares.kind')}
        <select value={form.intent} disabled={editing !== null} onChange={(e) => setForm({ ...blank(e.target.value as Intent), name: form.name })}>
          {INTENTS.map((i) => (
            <option key={i} value={i}>
              {t(`traefik:intent.${i}`)}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t('traefik:middlewares.name')}
        <input value={form.name} required disabled={editing !== null} pattern="[a-z0-9][a-z0-9\-]{0,39}" onChange={(e) => set({ name: e.target.value })} />
      </label>
      {form.intent === 'local_only' && (
        <label>
          {t('traefik:middlewares.ranges')}
          <textarea rows={5} value={form.ranges} onChange={(e) => set({ ranges: e.target.value })} />
        </label>
      )}
      {form.intent === 'basic_auth' && (
        <fieldset>
          <legend>{t('traefik:middlewares.users')}</legend>
          {form.users.map((u, i) => (
            <div key={i}>
              <input aria-label={t('traefik:middlewares.username')} value={u.username} required onChange={(e) => set({ users: form.users.map((x, j) => (j === i ? { ...x, username: e.target.value } : x)) })} />{' '}
              <input
                aria-label={t('traefik:middlewares.password')}
                type="password"
                autoComplete="new-password"
                minLength={8}
                maxLength={72}
                required={!u.has_password}
                placeholder={u.has_password ? t('traefik:middlewares.keepPassword') : ''}
                value={u.password}
                onChange={(e) => set({ users: form.users.map((x, j) => (j === i ? { ...x, password: e.target.value } : x)) })}
              />{' '}
              {form.users.length > 1 && (
                <button type="button" onClick={() => set({ users: form.users.filter((_, j) => j !== i) })}>
                  {t('traefik:middlewares.removeUser')}
                </button>
              )}
            </div>
          ))}
          <button type="button" onClick={() => set({ users: [...form.users, { username: '', password: '' }] })}>
            {t('traefik:middlewares.addUser')}
          </button>
          <p className="hint">{t('traefik:middlewares.hashed')}</p>
        </fieldset>
      )}
      {form.intent === 'security_headers' && (
        <>
          <label>
            {t('traefik:middlewares.hsts')}
            <select value={form.hstsSeconds} onChange={(e) => set({ hstsSeconds: e.target.value })}>
              <option value="0">{t('traefik:middlewares.hstsOff')}</option>
              <option value="31536000">{t('traefik:middlewares.hstsYear')}</option>
            </select>
          </label>
          <label><input type="checkbox" checked={form.hstsSubdomains} onChange={(e) => set({ hstsSubdomains: e.target.checked })} /> {t('traefik:middlewares.subdomains')}</label>
          <label><input type="checkbox" checked={form.frameDeny} onChange={(e) => set({ frameDeny: e.target.checked })} /> {t('traefik:middlewares.frameDeny')}</label>
          <label><input type="checkbox" checked={form.nosniff} onChange={(e) => set({ nosniff: e.target.checked })} /> {t('traefik:middlewares.nosniff')}</label>
          <label>
            {t('traefik:middlewares.referrer')}
            <select value={form.referrer} onChange={(e) => set({ referrer: e.target.value })}>
              {['no-referrer', 'same-origin', 'strict-origin', 'strict-origin-when-cross-origin'].map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <label>
            {t('traefik:middlewares.csp')}
            <input value={form.csp} maxLength={1000} onChange={(e) => set({ csp: e.target.value })} />
          </label>
        </>
      )}
      {form.intent === 'redirect_https' && (
        <label><input type="checkbox" checked={form.permanent} onChange={(e) => set({ permanent: e.target.checked })} /> {t('traefik:middlewares.permanent')}</label>
      )}
      {form.intent === 'strip_prefix' && (
        <label>
          {t('traefik:middlewares.prefixes')}
          <textarea rows={3} required value={form.prefixes} placeholder="/app" onChange={(e) => set({ prefixes: e.target.value })} />
        </label>
      )}
      {form.intent === 'rate_limit' && (
        <>
          <label>{t('traefik:middlewares.average')}<input type="number" min={1} value={form.average} onChange={(e) => set({ average: e.target.value })} /></label>
          <label>{t('traefik:middlewares.burst')}<input type="number" min={1} value={form.burst} onChange={(e) => set({ burst: e.target.value })} /></label>
          <label>{t('traefik:middlewares.period')}<input type="number" min={1} max={3600} value={form.period} onChange={(e) => set({ period: e.target.value })} /></label>
        </>
      )}
      {form.intent === 'compress' && (
        <label>{t('traefik:middlewares.minBytes')}<input type="number" min={0} value={form.minBytes} onChange={(e) => set({ minBytes: e.target.value })} /></label>
      )}
      {form.intent === 'forward_auth' && (
        <>
          <label>{t('traefik:middlewares.address')}<input type="url" required value={form.address} placeholder="https://auth.example.com/verify" onChange={(e) => set({ address: e.target.value })} /></label>
          <label>{t('traefik:middlewares.responseHeaders')}<input value={form.responseHeaders} placeholder="X-User, X-Email" onChange={(e) => set({ responseHeaders: e.target.value })} /></label>
          <label><input type="checkbox" checked={form.trustForward} onChange={(e) => set({ trustForward: e.target.checked })} /> {t('traefik:middlewares.trustForward')}</label>
        </>
      )}
      <button type="button" onClick={() => preview.mutate()} disabled={preview.isPending}>{t('traefik:middlewares.preview')}</button>{' '}
      <button type="submit" className="primary" disabled={save.isPending}>{t('traefik:middlewares.save')}</button>{' '}
      <button type="button" onClick={onCancel}>{t('traefik:middlewares.cancel')}</button>
      {shown && <pre className="output">{shown}</pre>}
    </form>
  )
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'
import { useConfirm } from '../../components/Confirm'

const INTENTS = ['local_only', 'basic_auth', 'security_headers', 'redirect_https', 'strip_prefix', 'rate_limit', 'compress', 'forward_auth'] as const
type Intent = (typeof INTENTS)[number]

interface UserRow {
  username: string
  password: string
  has_password?: boolean
}

// What the form holds: every field as text or a simple value, turned into the API's body below.
interface Form {
  intent: Intent
  name: string
  ranges: string
  users: UserRow[]
  removeHeader: boolean
  hstsSeconds: string
  hstsSubdomains: boolean
  frameDeny: boolean
  nosniff: boolean
  referrer: string
  csp: string
  permanent: boolean
  prefixes: string
  average: string
  burst: string
  period: string
  minBytes: string
  address: string
  trustForward: boolean
  responseHeaders: string
}

interface Item {
  name: string
  sha256: string
  editable: boolean
  used_by: string[]
  spec: Record<string, unknown> & { intent: Intent; name: string }
}

const PRIVATE = ['127.0.0.1/32', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7']
const blank = (intent: Intent = 'local_only'): Form => ({
  intent, name: '', ranges: PRIVATE.join('\n'), users: [{ username: '', password: '' }], removeHeader: true,
  hstsSeconds: '31536000', hstsSubdomains: false, frameDeny: true, nosniff: true, referrer: 'strict-origin-when-cross-origin', csp: '',
  permanent: true, prefixes: '', average: '100', burst: '50', period: '1', minBytes: '1024', address: '', trustForward: false, responseHeaders: '',
})

const lines = (text: string) => text.split(/[\n,]/).map((x) => x.trim()).filter(Boolean)

// The API's body for a form. A password travels once, here, and is never shown again.
function body(f: Form): Record<string, unknown> {
  const base = { intent: f.intent, name: f.name }
  switch (f.intent) {
    case 'local_only': return { ...base, ranges: lines(f.ranges) }
    case 'basic_auth': return { ...base, remove_header: f.removeHeader, users: f.users.map((u) => ({ username: u.username, ...(u.password ? { password: u.password } : {}) })) }
    case 'security_headers': return { ...base, hsts_seconds: Number(f.hstsSeconds), hsts_subdomains: f.hstsSubdomains, frame_deny: f.frameDeny, nosniff: f.nosniff, referrer_policy: f.referrer, csp: f.csp || null }
    case 'redirect_https': return { ...base, permanent: f.permanent }
    case 'strip_prefix': return { ...base, prefixes: lines(f.prefixes) }
    case 'rate_limit': return { ...base, average: Number(f.average), burst: Number(f.burst), period_seconds: Number(f.period) }
    case 'compress': return { ...base, min_bytes: Number(f.minBytes) }
    case 'forward_auth': return { ...base, address: f.address, trust_forward_header: f.trustForward, response_headers: lines(f.responseHeaders) }
  }
}

// The form for an existing middleware (passwords are never part of it: they are empty).
function fromSpec(spec: Item['spec']): Form {
  const f = blank(spec.intent)
  const s = spec as Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any
  f.name = spec.name
  if (spec.intent === 'local_only') f.ranges = (s.ranges as string[]).join('\n')
  if (spec.intent === 'basic_auth') {
    f.removeHeader = s.remove_header
    f.users = (s.users as { username: string; has_password: boolean }[]).map((u) => ({ username: u.username, password: '', has_password: u.has_password }))
  }
  if (spec.intent === 'security_headers') Object.assign(f, { hstsSeconds: String(s.hsts_seconds), hstsSubdomains: s.hsts_subdomains, frameDeny: s.frame_deny, nosniff: s.nosniff, referrer: s.referrer_policy, csp: s.csp ?? '' })
  if (spec.intent === 'redirect_https') f.permanent = s.permanent
  if (spec.intent === 'strip_prefix') f.prefixes = (s.prefixes as string[]).join('\n')
  if (spec.intent === 'rate_limit') Object.assign(f, { average: String(s.average), burst: String(s.burst), period: String(s.period_seconds) })
  if (spec.intent === 'compress') f.minBytes = String(s.min_bytes)
  if (spec.intent === 'forward_auth') Object.assign(f, { address: s.address, trustForward: s.trust_forward_header, responseHeaders: (s.response_headers as string[]).join(', ') })
  return f
}

// Middlewares made from an intention: pick what you want ("only my network", "ask for a
// password"...) and the file is written for you. Passwords are hashed on the server.
export function Middlewares({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const confirm = useConfirm()
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/traefik/middlewares`
  const [form, setForm] = useState<Form | null>(null)
  const [editing, setEditing] = useState<Item | null>(null)
  const [shown, setShown] = useState<string | null>(null)
  const list = useQuery({ queryKey: ['traefik-middlewares', nodeId], queryFn: () => api.get<{ middlewares: Item[] }>(base) })
  const refresh = () => client.invalidateQueries({ queryKey: ['traefik-middlewares', nodeId] })
  const done = () => {
    setForm(null)
    setEditing(null)
    setShown(null)
    return refresh()
  }
  const save = useMutation({
    mutationFn: () => (editing ? api.put(`${base}/${editing.name}`, { spec: body(form!), expected_sha256: editing.sha256 }) : api.post(base, { spec: body(form!) })),
    onSuccess: done,
  })
  const preview = useMutation({
    mutationFn: () => api.post<{ content: string }>(`${base}/preview`, { spec: body(form!) }),
    onSuccess: (data) => setShown(data.content),
  })
  // Renaming follows the sites the form built; the plan is shown first, and hand-written files
  // that name the middleware are listed because nothing rewrites them.
  const [renaming, setRenaming] = useState<{ item: Item; name: string } | null>(null)
  const plan = useMutation({
    mutationFn: () => api.post<{ sites: string[]; manual: string[] }>(`${base}/${renaming!.item.name}/rename`, { new_name: renaming!.name, expected_sha256: renaming!.item.sha256, dry_run: true }),
  })
  const rename = useMutation({
    mutationFn: () => api.post(`${base}/${renaming!.item.name}/rename`, { new_name: renaming!.name, expected_sha256: renaming!.item.sha256 }),
    onSuccess: () => {
      setRenaming(null)
      plan.reset()
      return client.invalidateQueries({ queryKey: ['traefik-middlewares', nodeId] }).then(() => Promise.all([client.invalidateQueries({ queryKey: ['traefik-sites', nodeId] }), client.invalidateQueries({ queryKey: ['traefik-managed', nodeId] })]))
    },
  })
  const remove = useMutation({ mutationFn: (m: Item) => api.del(`${base}/${m.name}?expected_sha256=${m.sha256}`), onSuccess: refresh })
  const failed = list.error ?? save.error ?? preview.error ?? remove.error ?? plan.error ?? rename.error

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate()
  }
  const set = (patch: Partial<Form>) => setForm({ ...form!, ...patch })

  return (
    <section aria-label={t('traefik:middlewares.title')}>
      <h2>{t('traefik:middlewares.title')}</h2>
      <p>{t('traefik:middlewares.explain')}</p>
      {failed != null && (
        <Problem error={failed} />
      )}
      {list.data?.middlewares.length === 0 && <p className="empty">{t('traefik:middlewares.none')}</p>}
      <ul className="links">
        {(list.data?.middlewares ?? []).map((m) => (
          <li key={m.name}>
            <strong>{m.name}</strong> <span className="badge off">{t(`traefik:intent.${m.spec.intent}`)}</span>{' '}
            {m.used_by.length > 0 ? <small>{t('traefik:middlewares.usedBy', { sites: m.used_by.join(', ') })}</small> : <small>{t('traefik:middlewares.unused')}</small>}{' '}
            {m.editable ? (
              <button type="button" onClick={() => { setEditing(m); setForm(fromSpec(m.spec)); setShown(null) }}>
                {t('traefik:middlewares.edit')}
              </button>
            ) : (
              <span className="notice">{t('traefik:middlewares.byHand')}</span>
            )}{' '}
            {m.editable && (
              <>
                <button type="button" onClick={() => { setRenaming({ item: m, name: m.name }); plan.reset() }}>
                  {t('traefik:middlewares.rename')}
                </button>{' '}
              </>
            )}
            <button type="button" disabled={m.used_by.length > 0} title={m.used_by.length > 0 ? t('traefik:middlewares.inUse') : undefined} onClick={async () => (await confirm(t('traefik:middlewares.confirmDelete'))) && remove.mutate(m)}>
              {t('traefik:middlewares.delete')}
            </button>
          </li>
        ))}
      </ul>
      {renaming && (
        <div className="notice" role="group" aria-label={t('traefik:middlewares.renameTitle')}>
          <label>
            {t('traefik:middlewares.newName')}
            <input value={renaming.name} maxLength={64} onChange={(e) => { setRenaming({ ...renaming, name: e.target.value }); plan.reset() }} />
          </label>{' '}
          <button type="button" disabled={plan.isPending || renaming.name === renaming.item.name || renaming.name === ''} onClick={() => plan.mutate()}>
            {t('traefik:middlewares.renamePlan')}
          </button>{' '}
          <button type="button" onClick={() => { setRenaming(null); plan.reset() }}>
            {t('traefik:middlewares.renameCancel')}
          </button>
          {plan.data && (
            <>
              <p>{t('traefik:middlewares.renameSites', { count: plan.data.sites.length, sites: plan.data.sites.join(', ') })}</p>
              {plan.data.manual.length > 0 && (
                <p role="alert" className="error">
                  {t('traefik:middlewares.renameManual', { files: plan.data.manual.join(', ') })}
                </p>
              )}
              <button type="button" disabled={rename.isPending} onClick={() => rename.mutate()}>
                {t('traefik:middlewares.renameGo')}
              </button>
            </>
          )}
        </div>
      )}
      {!form && (
        <button type="button" onClick={() => setForm(blank())}>
          {t('traefik:middlewares.new')}
        </button>
      )}
      {form && (
        <form onSubmit={submit} aria-label={t('traefik:middlewares.form')}>
          <h3>{editing ? t('traefik:middlewares.editing', { name: editing.name }) : t('traefik:middlewares.new')}</h3>
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
          <button type="button" onClick={() => { setForm(null); setEditing(null); setShown(null) }}>{t('traefik:middlewares.cancel')}</button>
          {shown && <pre className="output">{shown}</pre>}
        </form>
      )}
    </section>
  )
}

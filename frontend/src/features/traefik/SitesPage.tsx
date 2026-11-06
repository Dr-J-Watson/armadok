// The sites of the node as cards: the address, the protections, where it leads, whether its DNS
// record and certificate are in place. Sites made with the form can be changed, paused, renamed
// and deleted here; sites written by hand in a file open that file. Reading changes nothing.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Globe, History, Lock, MoreHorizontal, Pause, Pencil, Play, Plus, Search, Trash2, Unlink, Waypoints } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Problem } from '../../components/Problem'
import { Tip } from '../../components/Tip'
import { useConfirm } from '../../components/Confirm'
import { api } from '../../core/api'
import { SiteFollowUp } from './SiteFollowUp'
import { SiteForm } from './SiteForm'
import type { SiteSpec } from './SiteForm'

interface Router {
  name: string
  id: string
  rule: string
  service: string
  middlewares: string[]
  tls: boolean
  path: string
}
interface Sites {
  routers: Router[]
  services: { id: string; urls: string[] }[]
  errors: { path: string; error: string }[]
}
interface Managed {
  name: string
  paused: boolean
  sha256: string
  spec: SiteSpec | null
  modified_by_hand: boolean
}
interface Host {
  host: string
  status: 'present' | 'missing' | 'unmanaged' | 'no_domain' | 'error'
}
interface Certificate {
  names: string[]
  days_left: number
  state: 'ok' | 'expiring' | 'expired'
  not_after: string
  issuer: string
}
interface Version {
  version: string
  at: number
}

// One card, whichever way the site is defined.
interface Card {
  key: string
  title: string
  hosts: string[]
  rule: string
  middlewares: string[]
  target: string
  path: string | null
  paused: boolean
  managed: Managed | null
  routerId: string | null
}

const HOST = /Host\(([^)]*)\)/g
const hostsOf = (rule: string) => [...rule.matchAll(HOST)].flatMap((m) => [...m[1].matchAll(/`([^`]+)`/g)].map((x) => x[1]))
const strip = (name: string) => name.replace(/@file$/, '')

export function SitesPage({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'dns', 'errors'])
  const confirm = useConfirm()
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/traefik`
  const [filter, setFilter] = useState('')
  const [form, setForm] = useState<Managed | 'new' | null>(null)
  const [created, setCreated] = useState<{ hosts: string[]; https: boolean } | null>(null)
  const [renaming, setRenaming] = useState<{ name: string; to: string; sha256: string } | null>(null)
  const [historyOf, setHistoryOf] = useState<Managed | null>(null)

  const sites = useQuery({ queryKey: ['traefik-sites', nodeId], queryFn: () => api.get<Sites>(`${base}/sites`) })
  const managed = useQuery({ queryKey: ['traefik-managed', nodeId], queryFn: () => api.get<{ sites: Managed[] }>(`${base}/managed`), retry: false })
  const dns = useQuery({ queryKey: ['dns-report', nodeId], queryFn: () => api.get<{ hosts: Host[] }>(`/nodes/${nodeId}/dns/report`), retry: false })
  const certs = useQuery({ queryKey: ['certs', nodeId], queryFn: () => api.get<{ certificates: Certificate[] }>(`/nodes/${nodeId}/certs`), retry: false, staleTime: 60_000 })
  const versions = useQuery({
    queryKey: ['traefik-history', nodeId, historyOf?.name],
    enabled: historyOf !== null,
    queryFn: () => api.get<{ versions: Version[] }>(`${base}/history/sites/${historyOf!.name}.yml`),
  })

  const refresh = async () => {
    setRenaming(null)
    await Promise.all(['traefik-managed', 'traefik-sites', 'traefik-history', 'dns-report'].map((k) => client.invalidateQueries({ queryKey: [k, nodeId] })))
  }
  const act = useMutation({ mutationFn: (run: () => Promise<unknown>) => run(), onSuccess: refresh })
  const fixDns = useMutation({
    mutationFn: async (hosts: string[]) => {
      for (const host of hosts) await api.post(`/nodes/${nodeId}/dns/fix`, { host })
    },
    onSuccess: () => client.invalidateQueries({ queryKey: ['dns-report', nodeId] }),
  })

  const managedByName = useMemo(() => new Map((managed.data?.sites ?? []).map((s) => [s.name, s])), [managed.data])
  const cards: Card[] = useMemo(() => {
    const result: Card[] = []
    const services = new Map((sites.data?.services ?? []).map((s) => [s.id, s.urls]))
    for (const r of sites.data?.routers ?? []) {
      const own = /^sites\/([a-z0-9-]+)\.yml$/.exec(r.path)?.[1]
      const hosts = hostsOf(r.rule)
      result.push({
        key: `${r.path}:${r.id}`,
        title: hosts[0] ?? r.name,
        hosts,
        rule: r.rule,
        middlewares: r.middlewares.map(strip),
        target: (services.get(r.service) ?? []).join(', ') || strip(r.service),
        path: r.path,
        paused: false,
        managed: own ? (managedByName.get(own) ?? null) : null,
        routerId: r.id,
      })
    }
    // A paused site is not loaded by Traefik, so it is described from what the form saved.
    for (const s of managed.data?.sites ?? []) {
      if (!s.paused || !s.spec) continue
      result.push({
        key: `paused:${s.name}`,
        title: s.spec.hosts[0] ?? s.name,
        hosts: s.spec.hosts,
        rule: s.spec.hosts.map((h) => `Host(\`${h}\`)`).join(' || '),
        middlewares: s.spec.middlewares.map(strip),
        target: s.spec.url ?? `${s.spec.container ?? ''}:${s.spec.port ?? ''}`,
        path: null,
        paused: true,
        managed: s,
        routerId: null,
      })
    }
    return result.sort((a, b) => a.title.localeCompare(b.title))
  }, [sites.data, managed.data, managedByName])

  const needle = filter.trim().toLowerCase()
  const shown = cards.filter((c) => !needle || `${c.title} ${c.rule} ${c.target}`.toLowerCase().includes(needle))
  const dnsOf = new Map((dns.data?.hosts ?? []).map((h) => [h.host, h.status]))
  const missingHosts = (dns.data?.hosts ?? []).filter((h) => h.status === 'missing').map((h) => h.host)
  const paused = cards.filter((c) => c.paused).length
  const failed = sites.error ?? act.error ?? fixDns.error

  const certFor = (host: string) => (certs.data?.certificates ?? []).find((c) => c.names.some((n) => n === host || (n.startsWith('*.') && host.endsWith(n.slice(1)) && host.split('.').length === n.split('.').length)))

  if (created) return <SiteFollowUp nodeId={nodeId} {...created} onDone={() => setCreated(null)} />
  if (form)
    return (
      <SiteForm
        key={form === 'new' ? 'new' : form.name}
        nodeId={nodeId}
        initial={form === 'new' ? undefined : (form.spec ?? undefined)}
        expectedSha256={form === 'new' ? undefined : form.sha256}
        onDone={(made) => {
          setForm(null)
          if (made) setCreated(made)
          void refresh()
        }}
      />
    )

  const editable = (c: Card) => c.managed !== null && !c.paused && !c.managed.modified_by_hand
  const doDelete = async (c: Card) => {
    if (c.managed && (await confirm(t('traefik:sites.deleteConfirm', { name: c.title }))))
      act.mutate(() => api.del(`${base}/sites/${c.managed!.name}?expected_sha256=${c.managed!.sha256}`))
  }

  return (
    <>
      <header className="page-head page-head-split">
        <div>
          <h1>
            {t('traefik:sites.title')}
            <Tip text={t('traefik:sites.tip')} />
          </h1>
          <p className="hint">
            {t('traefik:sites.subtitle')} (<Link to={`/nodes/${nodeId}?tab=middlewares`}>{t('traefik:sites.manage')}</Link>)
            {paused > 0 && <> · <b>{t('traefik:sites.paused', { count: paused })}</b></>}
          </p>
        </div>
        <button type="button" className="primary" onClick={() => setForm('new')}>
          <Plus size={16} aria-hidden="true" /> {t('traefik:managed.new')}
        </button>
      </header>
      {failed != null && <Problem error={failed} />}
      {missingHosts.length > 0 && (
        <div className="banner" role="status">
          <Unlink size={18} aria-hidden="true" />
          <span>{t('traefik:sites.missingDns', { count: missingHosts.length })}</span>
          <button type="button" className="primary" disabled={fixDns.isPending} onClick={() => fixDns.mutate(missingHosts)}>
            <Plus size={13} aria-hidden="true" /> {t('traefik:sites.addDns')}
          </button>
        </div>
      )}
      {(sites.data?.errors ?? []).map((e) => (
        <p key={e.path} className="notice" role="alert">
          {t('traefik:badFile', { path: e.path })}{' '}
          <Link to={`/nodes/${nodeId}?tab=traefik-advanced&view=files&file=${encodeURIComponent(e.path)}`}>{t('traefik:openFile')}</Link>
        </p>
      ))}
      <label className="search-box">
        <Search size={15} aria-hidden="true" />
        <span className="visually-hidden">{t('traefik:searchSites')}</span>
        <input type="search" placeholder={t('traefik:searchSites')} value={filter} onChange={(e) => setFilter(e.target.value)} />
      </label>
      {sites.isPending && <p className="loading">{t('loading')}</p>}
      {sites.isSuccess && cards.length === 0 && (
        <div className="empty">
          <p>{t('traefik:sites.none')}</p>
          <button type="button" className="primary" onClick={() => setForm('new')}>
            <Plus size={14} aria-hidden="true" /> {t('traefik:sites.createFirst')}
          </button>
        </div>
      )}
      {cards.length > 0 && shown.length === 0 && <p className="empty">{t('traefik:noSiteMatch')}</p>}
      <ul className="site-grid">
        {shown.map((c) => {
          const statuses = c.hosts.map((h) => dnsOf.get(h)).filter((s): s is Host['status'] => s !== undefined)
          const cert = c.hosts.length > 0 ? certFor(c.hosts[0]) : undefined
          const m = c.managed
          return (
            <li key={c.key} className={`site-card${c.paused ? ' paused' : ''}`}>
              <header>
                <Globe size={16} aria-hidden="true" className="muted" />
                <span className="grow ellipsis">
                  {editable(c) ? (
                    <button type="button" className="link-button strong" onClick={() => setForm(m!)}>{c.title}</button>
                  ) : (
                    c.title
                  )}
                </span>
                {c.paused && <span className="badge warn"><Pause size={10} aria-hidden="true" /> {t('traefik:managed.paused')}</span>}
                {c.hosts.length > 1 && <span className="badge">+{c.hosts.length - 1}</span>}
                {c.routerId && (
                  <Link to={`/nodes/${nodeId}?tab=map&focus=${encodeURIComponent(`router:${c.routerId}`)}`} title={t('traefik:relations')} aria-label={t('traefik:relations')}>
                    <Waypoints size={14} aria-hidden="true" />
                  </Link>
                )}
                {c.hosts[0] && !c.paused && (
                  <a href={`https://${c.hosts[0]}`} target="_blank" rel="noopener noreferrer" title={t('traefik:sites.open')} aria-label={t('traefik:sites.open')}>
                    <ExternalLink size={14} aria-hidden="true" />
                  </a>
                )}
              </header>
              <p className="hint mono ellipsis" title={c.rule}>{c.rule}</p>
              <p className="chips">
                {c.middlewares.length === 0 ? (
                  <span className="badge warn" title={t('traefik:sites.noProtectionTip')}><Lock size={11} aria-hidden="true" /> {t('traefik:sites.noProtection')}</span>
                ) : (
                  c.middlewares.map((x) => <span key={x} className="badge info">{x}</span>)
                )}
              </p>
              <p className="target">
                <span className="muted">→</span> <code className="ellipsis" title={c.target}>{c.target}</code>
                <span className="grow" />
                {statuses.some((s) => s === 'missing') && <span className="badge warn" title={t('traefik:sites.dnsMissingTip')}>{t('traefik:sites.dnsMissing')}</span>}
                {cert && !c.paused && (
                  <span className={`badge ${cert.state === 'ok' ? 'ok' : cert.state === 'expiring' ? 'warn' : 'danger-badge'}`} title={t('traefik:sites.certTip', { date: new Date(cert.not_after).toLocaleDateString(), issuer: cert.issuer })}>
                    <Lock size={10} aria-hidden="true" /> {cert.state === 'expired' ? t('certs:expired') : t('traefik:sites.days', { count: cert.days_left })}
                  </span>
                )}
                {statuses.length > 0 && statuses.every((s) => s === 'present') && <span className="badge ok" title={t('traefik:sites.dnsOkTip')}>{t('traefik:sites.dnsOk')}</span>}
              </p>
              <footer>
                {m ? (
                  <>
                    {c.paused ? (
                      <button type="button" className="primary" onClick={() => act.mutate(() => api.post(`${base}/sites/${m.name}/resume`))}>
                        <Play size={13} aria-hidden="true" /> {t('traefik:managed.resume')}
                      </button>
                    ) : editable(c) ? (
                      <button type="button" onClick={() => setForm(m)}>
                        <Pencil size={13} aria-hidden="true" /> {t('traefik:managed.edit')}
                      </button>
                    ) : (
                      <span className="badge" title={t('traefik:managed.byHand')}>{t('traefik:managed.byHand')}</span>
                    )}
                    <span className="grow" />
                    {!c.paused && (
                      <button type="button" role="switch" aria-checked="true" className="switch on" title={t('traefik:managed.pauseTip')} aria-label={`${t('traefik:managed.pause')} ${c.title}`} onClick={() => act.mutate(() => api.post(`${base}/sites/${m.name}/pause`))} />
                    )}
                    <details className="menu">
                      <summary aria-label={t('traefik:sites.more')} title={t('traefik:sites.more')}><MoreHorizontal size={16} aria-hidden="true" /></summary>
                      <div className="menu-list">
                        {editable(c) && <button type="button" onClick={() => setRenaming({ name: m.name, to: '', sha256: m.sha256 })}>{t('traefik:managed.rename')}</button>}
                        <button type="button" onClick={() => setHistoryOf(historyOf?.name === m.name ? null : m)}><History size={13} aria-hidden="true" /> {t('traefik:managed.history')}</button>
                      </div>
                    </details>
                    <button type="button" className="icon-button danger" title={t('traefik:managed.delete')} aria-label={`${t('traefik:managed.delete')} ${c.title}`} onClick={() => void doDelete(c)}>
                      <Trash2 size={14} aria-hidden="true" />
                    </button>
                  </>
                ) : (
                  <>
                    <span className="badge" title={t('traefik:sites.fileTip')}>{t('traefik:sites.file')}</span>
                    <span className="grow" />
                    {c.path && (
                      <Link className="button-link small" to={`/nodes/${nodeId}?tab=traefik-advanced&view=files&file=${encodeURIComponent(c.path)}`}>
                        <Pencil size={13} aria-hidden="true" /> {t('traefik:mw.openFile')}
                      </Link>
                    )}
                  </>
                )}
              </footer>
              {renaming && m && renaming.name === m.name && (
                <form
                  onSubmit={(e) => {
                    e.preventDefault()
                    act.mutate(() => api.post(`${base}/sites/${m.name}/rename`, { new_name: renaming.to, expected_sha256: renaming.sha256 }))
                  }}
                >
                  <label>
                    {t('traefik:managed.newName')}
                    <input value={renaming.to} required pattern="[a-z0-9][a-z0-9\-]{0,39}" onChange={(e) => setRenaming({ ...renaming, to: e.target.value })} />
                  </label>
                  <div className="actions">
                    <button type="submit" className="primary">{t('traefik:managed.rename')}</button>
                    <button type="button" onClick={() => setRenaming(null)}>{t('traefik:managed.cancel')}</button>
                  </div>
                </form>
              )}
              {historyOf && m && historyOf.name === m.name && (
                <div>
                  {versions.isError ? (
                    <Problem error={versions.error} />
                  ) : versions.data && versions.data.versions.length === 0 ? (
                    <p className="empty">{t('traefik:managed.noHistory')}</p>
                  ) : (
                    <ul className="links">
                      {versions.data?.versions.map((v) => (
                        <li key={v.version}>
                          {new Date(v.at).toLocaleString()}{' '}
                          <button type="button" onClick={() => act.mutate(() => api.post(`${base}/restore/sites/${m.name}.yml`, { version: v.version, expected_sha256: m.sha256 }))}>
                            {t('traefik:managed.restore')}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </>
  )
}

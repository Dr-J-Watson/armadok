import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { Block } from '../../components/Block'
import { ArrowDown, ArrowUp, Cloud, Globe, Lock, Pencil, Plus, Route, Send, Shield, X } from 'lucide-react'
import { Drawer } from '../../components/Drawer'

export interface SiteSpec {
  name: string
  hosts: string[]
  path_prefix: string | null
  container: string | null
  url: string | null
  port: number | null
  scheme: 'http' | 'https'
  middlewares: string[]
  https: boolean
  cert_resolver: string | null
  entrypoints: string[] | null
  priority?: number | null
  servers?: string[]
  service?: string | null
  conditions?: Condition[]
}

export interface Condition {
  type: string
  value: string
  name?: string | null
}

const CONDITIONS = ['PathPrefix', 'Path', 'PathRegexp', 'Method', 'Header', 'HeaderRegexp', 'Query', 'QueryRegexp', 'ClientIP']
const NAMED = ['Header', 'HeaderRegexp', 'Query', 'QueryRegexp']

interface DockerContainer {
  names: string[]
  ports: { private: number | null }[]
}

const EMPTY: SiteSpec = {
  name: '',
  hosts: [],
  path_prefix: null,
  container: null,
  url: null,
  port: 80,
  scheme: 'http',
  middlewares: [],
  https: true,
  cert_resolver: null,
  entrypoints: null,
  priority: null,
  servers: [],
  service: null,
  conditions: [],
}

// The site builder: "when someone visits … apply … then send to … over HTTPS". The server
// writes the file; this form only describes the site, and can show the file first.
export function SiteForm({
  nodeId,
  initial,
  expectedSha256,
  onDone,
}: {
  nodeId: string
  initial?: SiteSpec
  expectedSha256?: string
  onDone: (created?: { hosts: string[]; https: boolean }) => void
}) {
  const { t } = useTranslation(['traefik', 'dns', 'errors'])
  const queryClient = useQueryClient()
  const editing = initial !== undefined
  const [spec, setSpec] = useState<SiteSpec>(initial ?? EMPTY)
  // One row per address; there is always at least one row to type in.
  const [hosts, setHosts] = useState<string[]>(initial?.hosts?.length ? initial.hosts : [''])
  const [entrypointsText, setEntrypointsText] = useState((initial?.entrypoints ?? []).join(', '))
  const [target, setTarget] = useState<'container' | 'address' | 'service'>(initial?.service ? 'service' : initial?.url != null ? 'address' : 'container')
  const byAddress = target === 'address'
  const [preview, setPreview] = useState<{ yaml: string; name_taken: boolean } | null>(null)

  // Suggestions only: the machine may have no Docker, and a name can be typed by hand.
  const containers = useQuery({
    queryKey: ['containers', nodeId],
    queryFn: () => api.get<{ containers: DockerContainer[] }>(`/nodes/${nodeId}/docker/containers`),
    retry: false,
  })
  // The node's defaults for new sites (entry points, certificate resolver). Optional: a person
  // who may not read them, or a hub without the module, simply gets no suggestion.
  const defaults = useQuery({
    queryKey: ['node-settings', nodeId],
    queryFn: () => api.get<{ traefik_entrypoints: string[]; traefik_resolver: string }>(`/nodes/${nodeId}/settings`),
    retry: false,
  })
  const defaultEntrypoints = defaults.data?.traefik_entrypoints ?? []
  const defaultResolver = defaults.data?.traefik_resolver ?? ''
  const known = useQuery({
    queryKey: ['traefik-sites', nodeId],
    queryFn: () => api.get<{ middlewares: { id: string }[]; services: { id: string }[] }>(`/nodes/${nodeId}/traefik/sites`),
  })
  const dns = useQuery({
    queryKey: ['dns-report', nodeId],
    queryFn: () => api.get<{ hosts: { host: string; status: string; resolves: string[] }[] }>(`/nodes/${nodeId}/dns/report`),
    retry: false,
  })

  const entered = entrypointsText.split(/[\s,]+/).filter(Boolean)
  const body = () => ({
    spec: {
      ...spec,
      hosts: hosts.map((h) => h.trim()).filter(Boolean),
      path_prefix: spec.path_prefix || null,
      container: target === 'container' ? spec.container : null,
      url: byAddress ? spec.url : null,
      port: target === 'container' ? spec.port : null,
      service: target === 'service' ? spec.service || null : null,
      servers: target === 'service' ? [] : (spec.servers ?? []).map((u) => u.trim()).filter(Boolean),
      priority: spec.priority || null,
      conditions: (spec.conditions ?? []).filter((c) => c.value.trim() !== ''),
      cert_resolver: spec.https ? spec.cert_resolver || (editing ? null : defaultResolver || null) : null,
      entrypoints: entered.length > 0 ? entered : editing ? null : defaultEntrypoints.length > 0 ? defaultEntrypoints : null,
    },
  })

  const show = useMutation({
    mutationFn: () =>
      api.post<{ yaml: string; name_taken: boolean }>(`/nodes/${nodeId}/traefik/sites/preview`, body()),
    onSuccess: setPreview,
  })
  const save = useMutation({
    mutationFn: () =>
      editing
        ? api.put(`/nodes/${nodeId}/traefik/sites/${spec.name}`, { ...body(), expected_sha256: expectedSha256 })
        : api.post(`/nodes/${nodeId}/traefik/sites`, body()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['traefik-managed', nodeId] })
      await queryClient.invalidateQueries({ queryKey: ['traefik-sites', nodeId] })
      onDone(editing ? undefined : { hosts: body().spec.hosts, https: spec.https })
    },
  })

  function pickContainer(name: string) {
    const found = containers.data?.containers.find((c) => c.names.includes(name))
    const port = found?.ports.find((p) => p.private)?.private
    setSpec((s) => ({ ...s, container: name, port: port ?? s.port }))
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate()
  }

  const known_middlewares = (known.data?.middlewares ?? []).map((m) => m.id)
  const addable = known_middlewares.filter((id) => !spec.middlewares.includes(id))
  const move = (index: number, by: -1 | 1) => {
    const next = [...spec.middlewares]
    const [item] = next.splice(index, 1)
    next.splice(index + by, 0, item)
    setSpec({ ...spec, middlewares: next })
  }
  const setHost = (index: number, value: string) => setHosts(hosts.map((h, i) => (i === index ? value : h)))
  const hasPath = spec.path_prefix !== null

  return (
    <Drawer
      title={spec.name || t(editing ? 'traefik:form.edit' : 'traefik:form.new')}
      badge="HTTP"
      onClose={() => onDone()}
      footer={
        <>
          <button type="button" onClick={() => show.mutate()} disabled={show.isPending}>
            {t('traefik:form.preview')}
          </button>
          <button type="button" onClick={() => onDone()}>
            {t('traefik:form.cancel')}
          </button>
          <button type="submit" form="site-form" className="primary" disabled={save.isPending}>
            {t('traefik:form.save')}
          </button>
        </>
      }
    >
    <form id="site-form" onSubmit={submit} aria-label={t(editing ? 'traefik:form.edit' : 'traefik:form.new')}>
      <div className="block-flow">
        <Block tone="name" icon={Pencil} title={t('traefik:block.name')} help={t('traefik:block.nameHelp')}>
          <div className="block-pair">
          <label>
            {t('traefik:form.name')}
            <input
              value={spec.name}
              disabled={editing}
              pattern="[a-z0-9][a-z0-9\-]{0,39}"
              required
              onChange={(e) => setSpec({ ...spec, name: e.target.value })}
            />
          </label>
          <label>
            {t('traefik:form.priority')}
            <input
              type="number"
              min={1}
              value={spec.priority ?? ''}
              placeholder={t('traefik:form.priorityAuto')}
              onChange={(e) => setSpec({ ...spec, priority: e.target.value === '' ? null : Number(e.target.value) })}
            />
          </label>
          </div>
        </Block>

        <Block tone="when" icon={Globe} title={t('traefik:block.when')} help={t('traefik:block.whenHelp')}>
          {hosts.map((host, index) => (
            <div className="block-row" key={index}>
              <Globe size={16} aria-hidden="true" />
              <input
                aria-label={t('traefik:form.host')}
                value={host}
                required={index === 0}
                placeholder="www.example.com"
                onChange={(e) => setHost(index, e.target.value)}
              />
              {hosts.length > 1 && (
                <button type="button" className="icon-button" aria-label={t('traefik:form.removeHost')} onClick={() => setHosts(hosts.filter((_, i) => i !== index))}>
                  <X size={16} aria-hidden="true" />
                </button>
              )}
            </div>
          ))}
          {hasPath && (
            <div className="block-row">
              <Route size={16} aria-hidden="true" />
              <input
                aria-label={t('traefik:form.pathPrefix')}
                value={spec.path_prefix ?? ''}
                placeholder="/api"
                onChange={(e) => setSpec({ ...spec, path_prefix: e.target.value })}
              />
              <button type="button" className="icon-button" aria-label={t('traefik:form.removePath')} onClick={() => setSpec({ ...spec, path_prefix: null })}>
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          )}
          {(spec.conditions ?? []).map((c, i) => (
            <div className="block-cond" key={i}>
              <select aria-label={t('traefik:form.conditionType')} value={c.type} onChange={(e) => setSpec({ ...spec, conditions: (spec.conditions ?? []).map((x, j) => (j === i ? { type: e.target.value, value: x.value, name: NAMED.includes(e.target.value) ? (x.name ?? '') : null } : x)) })}>
                {CONDITIONS.map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
              {NAMED.includes(c.type) ? (
                <input aria-label={t('traefik:form.conditionName')} placeholder={t('traefik:form.conditionName')} value={c.name ?? ''} onChange={(e) => setSpec({ ...spec, conditions: (spec.conditions ?? []).map((x, j) => (j === i ? { ...x, name: e.target.value } : x)) })} />
              ) : (
                <span />
              )}
              <input aria-label={t('traefik:form.conditionValue')} placeholder={t('traefik:form.conditionValue')} value={c.value} onChange={(e) => setSpec({ ...spec, conditions: (spec.conditions ?? []).map((x, j) => (j === i ? { ...x, value: e.target.value } : x)) })} />
              <button type="button" className="icon-button" aria-label={t('traefik:form.removeCondition')} onClick={() => setSpec({ ...spec, conditions: (spec.conditions ?? []).filter((_, j) => j !== i) })}>
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          ))}
          <div className="block-add">
            <button type="button" onClick={() => setHosts([...hosts, ''])}>
              <Plus size={14} aria-hidden="true" /> {t('traefik:form.addHost')}
            </button>
            {!hasPath && (
              <button type="button" onClick={() => setSpec({ ...spec, path_prefix: '' })}>
                <Plus size={14} aria-hidden="true" /> {t('traefik:form.addPath')}
              </button>
            )}
            <button type="button" onClick={() => setSpec({ ...spec, conditions: [...(spec.conditions ?? []), { type: 'Method', value: '' }] })}>
              <Plus size={14} aria-hidden="true" /> {t('traefik:form.addCondition')}
            </button>
          </div>
        </Block>

        <Block tone="apply" icon={Shield} title={t('traefik:block.apply')} help={t('traefik:block.applyHelp')}>
          {spec.middlewares.length === 0 && <p className="hint">{t('traefik:form.noMiddleware')}</p>}
          <ol className="block-steps">
            {spec.middlewares.map((id, index) => (
              <li className="block-row" key={id}>
                <span className="block-number" aria-hidden="true">{index + 1}</span>
                <strong>{id.replace(/@.*$/, '')}</strong>
                <span className="block-actions">
                  <button type="button" className="icon-button" disabled={index === 0} aria-label={t('traefik:form.moveUp', { name: id })} onClick={() => move(index, -1)}>
                    <ArrowUp size={16} aria-hidden="true" />
                  </button>
                  <button type="button" className="icon-button" disabled={index === spec.middlewares.length - 1} aria-label={t('traefik:form.moveDown', { name: id })} onClick={() => move(index, 1)}>
                    <ArrowDown size={16} aria-hidden="true" />
                  </button>
                  <button type="button" className="icon-button" aria-label={t('traefik:form.removeMiddleware', { name: id })} onClick={() => setSpec({ ...spec, middlewares: spec.middlewares.filter((m) => m !== id) })}>
                    <X size={16} aria-hidden="true" />
                  </button>
                </span>
              </li>
            ))}
          </ol>
          {addable.length > 0 && (
            <label className="block-add">
              <span className="visually-hidden">{t('traefik:form.addMiddleware')}</span>
              <select
                value=""
                onChange={(e) => e.target.value && setSpec({ ...spec, middlewares: [...spec.middlewares, e.target.value] })}
              >
                <option value="">+ {t('traefik:form.addMiddleware')}</option>
                {addable.map((id) => (
                  <option key={id} value={id}>
                    {id.replace(/@file$/, '')}
                  </option>
                ))}
              </select>
            </label>
          )}
        </Block>

        <Block tone="send" icon={Send} title={t('traefik:block.send')} help={t('traefik:block.sendHelp')}>
          <div className="block-switch" role="group" aria-label={t('traefik:form.target')}>
            <button type="button" aria-pressed={target === 'container'} className={target === 'container' ? 'primary' : undefined} onClick={() => setTarget('container')}>
              {t('traefik:form.targetContainer')}
            </button>
            <button type="button" aria-pressed={byAddress} className={byAddress ? 'primary' : undefined} onClick={() => setTarget('address')}>
              {t('traefik:form.targetAddress')}
            </button>
            <button type="button" aria-pressed={target === 'service'} className={target === 'service' ? 'primary' : undefined} onClick={() => setTarget('service')}>
              {t('traefik:form.targetService')}
            </button>
          </div>
          {target === 'service' ? (
            <label>
              {t('traefik:form.service')}
              <input list="site-services" value={spec.service ?? ''} placeholder="shared@file" onChange={(e) => setSpec({ ...spec, service: e.target.value })} />
              <datalist id="site-services">{(known.data?.services ?? []).map((x) => <option key={x.id} value={x.id} />)}</datalist>
            </label>
          ) : byAddress ? (
            <label>
              {t('traefik:form.address')}
              <input value={spec.url ?? ''} placeholder="http://192.0.2.10:8080" onChange={(e) => setSpec({ ...spec, url: e.target.value })} />
            </label>
          ) : (
            <div className="block-pair">
              <label>
                {t('traefik:form.container')}
                <input list="site-containers" value={spec.container ?? ''} onChange={(e) => pickContainer(e.target.value)} />
              </label>
              <datalist id="site-containers">
                {(containers.data?.containers ?? []).flatMap((c) => c.names).map((n) => (
                  <option key={n} value={n} />
                ))}
              </datalist>
              <label>
                {t('traefik:form.port')}
                <input
                  type="number"
                  min={1}
                  max={65535}
                  value={spec.port ?? ''}
                  onChange={(e) => setSpec({ ...spec, port: Number(e.target.value) || null })}
                />
              </label>
            </div>
          )}
 
          {target !== 'service' && (
            <div className="block-conditions">
              {(spec.servers ?? []).map((url, i) => (
                <div className="block-row" key={i}>
                  <Send size={16} aria-hidden="true" />
                  <input aria-label={t('traefik:form.extraServer')} value={url} placeholder="http://192.0.2.11:8080" onChange={(e) => setSpec({ ...spec, servers: (spec.servers ?? []).map((u, j) => (j === i ? e.target.value : u)) })} />
                  <button type="button" className="icon-button" aria-label={t('traefik:form.removeServer')} onClick={() => setSpec({ ...spec, servers: (spec.servers ?? []).filter((_, j) => j !== i) })}>
                    <X size={16} aria-hidden="true" />
                  </button>
                </div>
              ))}
              <div className="block-add">
                <button type="button" title={t('traefik:form.extraServerTip')} onClick={() => setSpec({ ...spec, servers: [...(spec.servers ?? []), ''] })}>
                  <Plus size={14} aria-hidden="true" /> {t('traefik:form.addServer')}
                </button>
              </div>
            </div>
          )}
        </Block>

        <Block tone="https" icon={Lock} title={t('traefik:block.https')} help={t('traefik:block.httpsHelp')}>
          <label className="check">
            <input type="checkbox" checked={spec.https} onChange={(e) => setSpec({ ...spec, https: e.target.checked })} />
            {t('traefik:form.https')}
          </label>
          <label>
            {t('traefik:form.entrypoints')}
            <input
              value={entrypointsText}
              placeholder={defaultEntrypoints.join(', ')}
              onChange={(e) => setEntrypointsText(e.target.value)}
            />
          </label>
          {spec.https && (
            <label>
              {t('traefik:form.resolver')}
              <input
                value={spec.cert_resolver ?? ''}
                placeholder={defaultResolver}
                onChange={(e) => setSpec({ ...spec, cert_resolver: e.target.value })}
              />
            </label>
          )}
        </Block>

        {hosts.some((h) => h.trim() !== '') && (dns.data?.hosts.length ?? 0) > 0 && (
          <Block tone="dns" icon={Cloud} title={t('traefik:block.dns')} help={t('traefik:block.dnsHelp')}>
            {hosts.filter((h) => h.trim() !== '').map((h) => {
              const row = dns.data?.hosts.find((x) => x.host === h.trim().toLowerCase())
              return (
                <p className="block-row" key={h}>
                  <code>{h.trim()}</code>
                  {row ? (
                    <span className={`badge ${row.status === 'present' ? 'ok' : row.status === 'missing' ? 'warn' : ''}`}>
                      {t(`dns:report.states.${row.status}`, { defaultValue: row.status })}
                      {(row.resolves ?? []).length > 0 && ` · ${(row.resolves ?? []).join(", ")}`}
                    </span>
                  ) : (
                    <span className="hint">{t('traefik:form.dnsLater')}</span>
                  )}
                </p>
              )
            })}
          </Block>
        )}
      </div>
      {preview?.name_taken && !editing && (
        <p className="notice" role="alert">
          {t('traefik:form.nameTaken')}
        </p>
      )}
      {preview && <pre className="output">{preview.yaml}</pre>}
      {(show.isError || save.isError) && (
        <p role="alert" className="error">
          {errorMessage(t, (show.error ?? save.error) as Error)}
        </p>
      )}
    </form>
    </Drawer>
  )
}

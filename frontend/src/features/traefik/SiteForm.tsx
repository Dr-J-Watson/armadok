import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

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
}

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
  const { t } = useTranslation(['traefik', 'errors'])
  const queryClient = useQueryClient()
  const editing = initial !== undefined
  const [spec, setSpec] = useState<SiteSpec>(initial ?? EMPTY)
  const [hostsText, setHostsText] = useState((initial?.hosts ?? []).join('\n'))
  const [byAddress, setByAddress] = useState(initial?.url != null)
  const [preview, setPreview] = useState<{ yaml: string; name_taken: boolean } | null>(null)

  // Suggestions only: the machine may have no Docker, and a name can be typed by hand.
  const containers = useQuery({
    queryKey: ['containers', nodeId],
    queryFn: () => api.get<{ containers: DockerContainer[] }>(`/nodes/${nodeId}/docker/containers`),
    retry: false,
  })
  const known = useQuery({
    queryKey: ['traefik-sites', nodeId],
    queryFn: () => api.get<{ middlewares: { id: string }[] }>(`/nodes/${nodeId}/traefik/sites`),
  })

  const body = () => ({
    spec: {
      ...spec,
      hosts: hostsText.split(/[\s,]+/).filter(Boolean),
      path_prefix: spec.path_prefix || null,
      container: byAddress ? null : spec.container,
      url: byAddress ? spec.url : null,
      port: byAddress ? null : spec.port,
      cert_resolver: spec.https ? spec.cert_resolver || null : null,
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

  const middlewares = known.data?.middlewares ?? []
  return (
    <form onSubmit={submit} aria-label={t(editing ? 'traefik:form.edit' : 'traefik:form.new')}>
      <h3>{t(editing ? 'traefik:form.edit' : 'traefik:form.new')}</h3>
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
        {t('traefik:form.hosts')}
        <textarea value={hostsText} required rows={2} onChange={(e) => setHostsText(e.target.value)} />
      </label>
      <label>
        {t('traefik:form.pathPrefix')}
        <input value={spec.path_prefix ?? ''} onChange={(e) => setSpec({ ...spec, path_prefix: e.target.value })} />
      </label>
      <fieldset>
        <legend>{t('traefik:form.target')}</legend>
        <label>
          <input type="radio" checked={!byAddress} onChange={() => setByAddress(false)} />
          {t('traefik:form.targetContainer')}
        </label>
        <label>
          <input type="radio" checked={byAddress} onChange={() => setByAddress(true)} />
          {t('traefik:form.targetAddress')}
        </label>
        {byAddress ? (
          <label>
            {t('traefik:form.address')}
            <input value={spec.url ?? ''} placeholder="http://192.0.2.10:8080" onChange={(e) => setSpec({ ...spec, url: e.target.value })} />
          </label>
        ) : (
          <>
            <label>
              {t('traefik:form.container')}
              <input
                list="site-containers"
                value={spec.container ?? ''}
                onChange={(e) => pickContainer(e.target.value)}
              />
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
          </>
        )}
      </fieldset>
      {middlewares.length > 0 && (
        <fieldset>
          <legend>{t('traefik:form.middlewares')}</legend>
          {middlewares.map((m) => (
            <label key={m.id}>
              <input
                type="checkbox"
                checked={spec.middlewares.includes(m.id)}
                onChange={(e) =>
                  setSpec({
                    ...spec,
                    middlewares: e.target.checked
                      ? [...spec.middlewares, m.id]
                      : spec.middlewares.filter((x) => x !== m.id),
                  })
                }
              />
              {m.id.replace(/@file$/, '')}
            </label>
          ))}
        </fieldset>
      )}
      <label>
        <input type="checkbox" checked={spec.https} onChange={(e) => setSpec({ ...spec, https: e.target.checked })} />
        {t('traefik:form.https')}
      </label>
      {spec.https && (
        <label>
          {t('traefik:form.resolver')}
          <input value={spec.cert_resolver ?? ''} onChange={(e) => setSpec({ ...spec, cert_resolver: e.target.value })} />
        </label>
      )}
      <div className="actions">
        <button type="button" onClick={() => show.mutate()} disabled={show.isPending}>
          {t('traefik:form.preview')}
        </button>
        <button type="submit" className="primary" disabled={save.isPending}>
          {t('traefik:form.save')}
        </button>
        <button type="button" onClick={() => onDone()}>
          {t('traefik:form.cancel')}
        </button>
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
  )
}

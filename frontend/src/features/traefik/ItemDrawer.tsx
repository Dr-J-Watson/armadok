// Edit one middleware in a drawer, whatever way it was written: the form drawn from its type, or
// the YAML itself. Only this item's lines are replaced in its file (see the item endpoints), so
// the rest of the file keeps its comments and order.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Drawer } from '../../components/Drawer'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import { Icon } from './MiddlewaresPage'
import { MIDDLEWARES, prune } from './middlewareSchema'
import { kindOf } from './middlewareKinds'
import { SchemaForm } from './SchemaForm'
import type { Names } from './SchemaForm'

export type DrawerTarget =
  | { kind: 'edit'; path: string; name: string }
  | { kind: 'new'; name: string; value: Record<string, unknown> }

const typeOf = (value: Record<string, unknown>) => Object.keys(value)[0] ?? ''

export function ItemDrawer({ nodeId, target, files, names, onClose }: { nodeId: string; target: DrawerTarget; files: string[]; names: Names; onClose: () => void }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/traefik/items`
  const editing = target.kind === 'edit'
  const [name, setName] = useState(target.name)
  const [path, setPath] = useState(editing ? target.path : '')
  const [view, setView] = useState<'form' | 'yaml'>('form')
  const [value, setValue] = useState<Record<string, unknown> | null>(editing ? null : target.value)
  const [yamlText, setYamlText] = useState('')
  const [sha, setSha] = useState<string | undefined>()
  const [parseError, setParseError] = useState<unknown>(null)

  const loaded = useQuery({
    queryKey: ['traefik-item', nodeId, editing ? target.path : '', target.name],
    enabled: editing,
    queryFn: async () => {
      const got = await api.get<{ yaml: string; sha256: string }>(`${base}?path=${encodeURIComponent(path)}&kind=middlewares&name=${encodeURIComponent(name)}`)
      const parsed = await api.post<{ value: Record<string, unknown> }>(`${base}/parse`, { yaml: got.yaml })
      setYamlText(got.yaml)
      setValue(parsed.value)
      setSha(got.sha256)
      return got
    },
    staleTime: 0,
    gcTime: 0,
  })

  const type = value ? typeOf(value) : ''
  const kind = kindOf(type)
  const catalogue = MIDDLEWARES[type]
  const forcedYaml = catalogue === undefined || catalogue.yamlOnly === true
  const shownView = forcedYaml ? 'yaml' : view

  // The form edits the object under the type's key; what the catalogue does not list stays there.
  const body = (): Record<string, unknown> | undefined => {
    if (!value) return undefined
    const cleaned = prune(value[type]) ?? {}
    return { [type]: cleaned }
  }

  async function show(next: 'form' | 'yaml') {
    setParseError(null)
    if (next === 'yaml' && value) {
      const out = await api.post<{ yaml: string }>(`${base}/dump`, { value: body() }).catch(() => null)
      if (out) setYamlText(out.yaml)
    }
    if (next === 'form' && view === 'yaml') {
      try {
        const parsed = await api.post<{ value: Record<string, unknown> }>(`${base}/parse`, { yaml: yamlText })
        setValue(parsed.value)
      } catch (error) {
        setParseError(error)
        return
      }
    }
    setView(next)
  }

  const save = useMutation({
    mutationFn: async () => {
      const payload = shownView === 'yaml' ? { yaml: yamlText } : { value: body() }
      if (editing) return api.put(base, { path, kind: 'middlewares', name, ...payload, expected_sha256: sha })
      return api.post(base, { kind: 'middlewares', name, ...(path ? { path } : {}), ...payload })
    },
    onSuccess: async () => {
      await Promise.all(['traefik-overview', 'traefik-sites', 'traefik-middlewares'].map((k) => client.invalidateQueries({ queryKey: [k, nodeId] })))
      onClose()
    },
  })
  const hash = async (username: string, password: string) => (await api.post<{ entry: string }>(`${base}/htpasswd`, { username, password })).entry
  const failed = loaded.error ?? save.error ?? parseError

  return (
    <Drawer
      title={name || t('traefik:item.new')}
      badge="HTTP"
      onClose={onClose}
      footer={
        <>
          {editing && <span className="hint">→ {path}</span>}
          <button type="button" onClick={onClose}>{t('traefik:middlewares.cancel')}</button>
          <button type="button" className="primary" disabled={save.isPending || value === null || name === ''} onClick={() => save.mutate()}>
            {t('traefik:middlewares.save')}
          </button>
        </>
      }
    >
      {loaded.isPending && editing && <p className="loading">{t('loading')}</p>}
      {failed != null && <Problem error={failed} />}
      {value && (
        <>
          <div className="row3">
            <label className="field">
              <span>{t('traefik:item.name')}</span>
              <input value={name} required disabled={editing} pattern="[a-z0-9][a-z0-9\-]{0,39}" onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="field">
              <span>{t('traefik:item.file')}</span>
              {editing ? (
                <input value={path} disabled />
              ) : (
                <select value={path} onChange={(e) => setPath(e.target.value)}>
                  <option value="">{t('traefik:item.ownFile', { name: name || '…' })}</option>
                  {files.map((f) => <option key={f} value={f}>{f}</option>)}
                </select>
              )}
            </label>
            <div className="field">
              <span>{t('traefik:item.view')}</span>
              <div className="segmented" role="group" aria-label={t('traefik:item.view')}>
                <button type="button" aria-pressed={shownView === 'form'} disabled={forcedYaml} onClick={() => void show('form')}>{t('traefik:item.form')}</button>
                <button type="button" aria-pressed={shownView === 'yaml'} onClick={() => void show('yaml')}>YAML</button>
              </div>
            </div>
          </div>
          <div className="type-head" data-group={kind.group}>
            <span className="mw-icon" aria-hidden="true"><Icon name={kind.icon} /></span>
            <div>
              <strong>{kind.label}</strong>
              <span className="hint">{t(`traefik:mw.type.${type}`, { defaultValue: type })}</span>
            </div>
          </div>
          {shownView === 'form' && catalogue ? (
            <SchemaForm
              fields={catalogue.fields}
              value={(value[type] as Record<string, unknown>) ?? {}}
              onChange={(next) => setValue({ ...value, [type]: next })}
              names={names}
              onHash={type === 'basicAuth' ? hash : undefined}
            />
          ) : (
            <>
              {forcedYaml && <p className="hint">{t('traefik:item.yamlOnly')}</p>}
              <textarea className="yaml-area" spellCheck={false} aria-label="YAML" value={yamlText} onChange={(e) => setYamlText(e.target.value)} />
            </>
          )}
        </>
      )}
    </Drawer>
  )
}

import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { codeMessage } from '../../core/errors'
import { readPref, writePref } from '../../core/storage'
import { GraphView } from './GraphView'
import { RelationsPanel } from './RelationsPanel'
import { buildElements, matchesSearch, objectKey, typesOf } from './graph'
import type { RelationsMap } from './graph'
import { Problem } from '../../components/Problem'

// Which types are hidden is remembered per node (a display preference, nothing else).
function hiddenKey(nodeId: string) {
  return `map.hidden.${nodeId}`
}

function loadHidden(nodeId: string): Set<string> {
  try {
    return new Set(JSON.parse(readPref(hiddenKey(nodeId)) ?? '[]') as string[])
  } catch {
    return new Set()
  }
}

export function MapTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['relations', 'errors'])
  const [params, setParams] = useSearchParams()
  const [hidden, setHidden] = useState(() => loadHidden(nodeId))
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<string | null>(params.get('focus'))

  const map = useQuery({
    queryKey: ['relations', nodeId],
    queryFn: () => api.get<RelationsMap>(`/nodes/${nodeId}/relations`),
    refetchInterval: 15000,
  })

  const types = useMemo(() => typesOf(map.data?.objects ?? []), [map.data])
  const visible = useMemo(() => new Set(types.filter((type) => !hidden.has(type))), [types, hidden])
  const elements = useMemo(() => (map.data ? buildElements(map.data, visible) : []), [map.data, visible])
  const matches = useMemo(
    () => (map.data?.objects ?? []).filter((o) => visible.has(o.type) && matchesSearch(o, search)),
    [map.data, visible, search],
  )
  const themeKey = `${document.documentElement.dataset.theme ?? 'system'}`
  const selectedObject = map.data?.objects.find((o) => objectKey(o) === selected)

  function toggle(type: string) {
    const next = new Set(hidden)
    if (next.has(type)) next.delete(type)
    else next.add(type)
    setHidden(next)
    writePref(hiddenKey(nodeId), JSON.stringify([...next]))
  }

  function select(key: string | null) {
    setSelected(key)
    // Keep the address in sync, so a selection can be shared and survives a reload.
    const next = new URLSearchParams(params)
    if (key) next.set('focus', key)
    else next.delete('focus')
    setParams(next, { replace: true })
  }

  if (map.isPending) return <p>{t('loading')}</p>
  if (map.isError) {
    return (
      <Problem error={map.error} />
    )
  }

  const unavailable = Object.entries(map.data.unavailable)
  return (
    <>
      {unavailable.map(([provider, code]) => (
        <p key={provider} className="notice" role="status">
          {t('relations:unavailable', { provider: t(`relations:providers.${provider}`, { defaultValue: provider }) })}{' '}
          {codeMessage(t, code)}
        </p>
      ))}
      {map.data.objects.length === 0 ? (
        <p className="empty">{t('relations:empty')}</p>
      ) : (
        <>
          <fieldset className="legend">
            <legend>{t('relations:show')}</legend>
            {types.map((type) => (
              <label key={type} className="legend-item">
                <input type="checkbox" checked={!hidden.has(type)} onChange={() => toggle(type)} />
                <span className="swatch" data-type={type} aria-hidden="true" />
                {t(`relations:types.${type}`, { defaultValue: type })}
              </label>
            ))}
          </fieldset>

          <div className="map-layout">
            <div>
              <GraphView elements={elements} selected={selected} onSelect={select} themeKey={themeKey} />
              <details className="object-list">
                <summary>{t('relations:list')}</summary>
                <label htmlFor="map-search" className="visually-hidden">
                  {t('relations:search')}
                </label>
                <input
                  id="map-search"
                  type="search"
                  placeholder={t('relations:search')}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                <ul>
                  {matches.map((o) => (
                    <li key={objectKey(o)}>
                      <button
                        type="button"
                        className="link-button"
                        aria-pressed={selected === objectKey(o)}
                        onClick={() => select(objectKey(o))}
                      >
                        {o.label}
                      </button>{' '}
                      <span className="badge">{t(`relations:types.${o.type}`, { defaultValue: o.type })}</span>
                    </li>
                  ))}
                </ul>
              </details>
            </div>

            <div>
              {selectedObject ? (
                <RelationsPanel
                  nodeId={nodeId}
                  object={selectedObject}
                  onSelect={select}
                  extra={
                    selectedObject.type === 'container' && (
                      <p>
                        <Link to={`/nodes/${nodeId}?tab=containers`}>{t('relations:openContainers')}</Link>
                      </p>
                    )
                  }
                />
              ) : (
                <p className="hint">{t('relations:pickOne')}</p>
              )}
            </div>
          </div>
        </>
      )}
    </>
  )
}

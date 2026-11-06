import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { codeMessage } from '../../core/errors'
import { readPref, writePref } from '../../core/storage'
import { GraphView } from './GraphView'
import { RelationsPanel } from './RelationsPanel'
import { buildElements, matchesSearch, neighbourhood, objectKey, typesOf } from './graph'
import type { RelationsMap } from './graph'
import { Problem } from '../../components/Problem'

// Which types are hidden is remembered per node (a display preference, nothing else).
function hiddenKey(nodeId: string) {
  return `map.hidden.v2.${nodeId}`
}

// null when the person never chose: the page then picks a sensible default for the size.
function loadHidden(nodeId: string): Set<string> | null {
  try {
    const stored = readPref(hiddenKey(nodeId))
    return stored === null ? null : new Set(JSON.parse(stored) as string[])
  } catch {
    return null
  }
}

// Images, networks and volumes are often shared by everything and bury the rest: hidden until asked for.
const NOISY = ['image', 'network', 'volume', 'service']

// The same value as before while its content is unchanged.
function useStable<T>(value: T): T {
  const [kept, setKept] = useState(value)
  const key = JSON.stringify(value)
  const [seen, setSeen] = useState(key)
  if (seen !== key) {
    setSeen(key)
    setKept(value)
  }
  return kept
}

export function MapTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['relations', 'errors'])
  const [params, setParams] = useSearchParams()
  const [chosen, setChosen] = useState(() => loadHidden(nodeId))
  const [search, setSearch] = useState('')
  const [only, setOnly] = useState(false)
  const [depth, setDepth] = useState(1)
  const [selected, setSelected] = useState<string | null>(params.get('focus'))

  const map = useQuery({
    queryKey: ['relations', nodeId],
    queryFn: () => api.get<RelationsMap>(`/nodes/${nodeId}/relations`),
    refetchInterval: 15000,
  })

  // The server is asked again every few seconds; the drawing is only rebuilt when the answer
  // actually differs, so it does not jump while someone reads it.
  const answer = useStable(map.data)
  const types = useMemo(() => typesOf(answer?.objects ?? []), [answer])
  const counts = useMemo(() => {
    const result: Record<string, number> = {}
    for (const o of answer?.objects ?? []) result[o.type] = (result[o.type] ?? 0) + 1
    return result
  }, [answer])
  const hidden = useMemo(() => chosen ?? new Set(NOISY), [chosen])
  const visible = useMemo(() => new Set(types.filter((type) => !hidden.has(type))), [types, hidden])
  const data = answer ?? map.data
  const elements = useMemo(() => {
    if (!data) return []
    return buildElements(only && selected ? neighbourhood(data, selected, depth, visible) : data, only && selected ? new Set(types) : visible)
  }, [data, visible, only, selected, depth, types])
  const matches = useMemo(
    () => (search.trim() ? (data?.objects ?? []).filter((o) => matchesSearch(o, search)) : []),
    [data, search],
  )
  const highlight = useMemo(() => (search.trim() ? new Set(matches.filter((o) => visible.has(o.type)).map(objectKey)) : null), [matches, visible, search])
  const themeKey = `${document.documentElement.dataset.theme ?? 'system'}`
  const selectedObject = data?.objects.find((o) => objectKey(o) === selected)

  function toggle(type: string) {
    const next = new Set(hidden)
    if (next.has(type)) next.delete(type)
    else next.add(type)
    setChosen(next)
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

  if (map.isPending) return <p className="loading">{t('loading')}</p>
  if (map.isError) return <Problem error={map.error} />

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
        <div className="map-page">
          <div className="map-toolbar">
            <div className="map-search">
              <label htmlFor="map-search" className="visually-hidden">
                {t('relations:search')}
              </label>
              <input id="map-search" type="search" placeholder={t('relations:search')} value={search} onChange={(e) => setSearch(e.target.value)} />
              {matches.length > 0 && (
                <ul className="map-results" aria-label={t('relations:results')}>
                  {matches.slice(0, 8).map((o) => (
                    <li key={objectKey(o)}>
                      <button
                        type="button"
                        onClick={() => {
                          if (hidden.has(o.type)) toggle(o.type) // a hidden kind would not be drawn
                          select(objectKey(o))
                          setSearch('')
                        }}
                      >
                        <span className="swatch" data-type={o.type} aria-hidden="true" /> {o.label}
                        <span className="muted"> · {t(`relations:types.${o.type}`, { defaultValue: o.type })}</span>
                      </button>
                    </li>
                  ))}
                  {matches.length > 8 && <li className="hint">{t('relations:moreResults', { count: matches.length - 8 })}</li>}
                </ul>
              )}
            </div>
            <fieldset className="pills-filter">
              <legend className="visually-hidden">{t('relations:show')}</legend>
              {types.map((type) => (
                <label key={type} className="pill-toggle">
                  <input type="checkbox" checked={!hidden.has(type)} onChange={() => toggle(type)} />
                  <span className="swatch" data-type={type} aria-hidden="true" />
                  {t(`relations:types.${type}`, { defaultValue: type })}
                  <span className="count" aria-hidden="true">{counts[type]}</span>
                </label>
              ))}
            </fieldset>
            <div className="map-focus">
              <button type="button" aria-pressed={only} disabled={!selected} title={t('relations:onlyTip')} onClick={() => setOnly(!only)}>
                {t('relations:only')}
              </button>
              {only && selected && (
                <select aria-label={t('relations:depth')} value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
                  <option value={1}>{t('relations:depth1')}</option>
                  <option value={2}>{t('relations:depth2')}</option>
                  <option value={3}>{t('relations:depth3')}</option>
                </select>
              )}
            </div>
          </div>

          <div className={`map-work${selectedObject ? ' has-drawer' : ''}`}>
            <GraphView elements={elements} selected={selected} onSelect={select} themeKey={themeKey} highlight={highlight} storageKey={nodeId} viewScope={only && selected ? `only:${selected}:${depth}` : 'all'} />
            {!selectedObject && <p className="map-empty hint">{t('relations:pickOne')}</p>}
            {selectedObject && (
              <div className="map-drawer">
                <button type="button" className="icon-button map-close" aria-label={t('relations:close')} title={t('relations:close')} onClick={() => select(null)}>
                  <X size={16} aria-hidden="true" />
                </button>
                <RelationsPanel
                  nodeId={nodeId}
                  object={selectedObject}
                  onSelect={select}
                  extra={
                    selectedObject.type === 'compose' ? (
                      <p className="notice">
                        {t('relations:notImported')} <Link to={`/nodes/${nodeId}?tab=projects`}>{t('relations:importIt')}</Link>
                      </p>
                    ) : (
                      selectedObject.type === 'container' && (
                        <p>
                          <Link to={`/nodes/${nodeId}?tab=containers`}>{t('relations:openContainers')}</Link>
                        </p>
                      )
                    )
                  }
                />
              </div>
            )}
          </div>

          <details className="object-list">
            <summary>{t('relations:list')}</summary>
            <ul>
              {(data?.objects ?? []).filter((o) => visible.has(o.type)).map((o) => (
                <li key={objectKey(o)}>
                  <button type="button" className="link-button" aria-pressed={selected === objectKey(o)} onClick={() => select(objectKey(o))}>
                    {o.label}
                  </button>{' '}
                  <span className="badge">{t(`relations:types.${o.type}`, { defaultValue: o.type })}</span>
                </li>
              ))}
            </ul>
          </details>
        </div>
      )}
    </>
  )
}

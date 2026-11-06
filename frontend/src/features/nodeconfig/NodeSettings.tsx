import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import { useAuth } from '../../core/auth'

interface Place {
  kind: 'dir' | 'file' | null
  root: string | null
  value: string
  valid: boolean
}

interface Settings {
  places: Record<string, Place>
  traefik_entrypoints: string[]
  traefik_resolver: string
  can_write: boolean
  error: string | null
}

const TRAEFIK_PLACES = ['traefik_dir', 'traefik_static_dir', 'acme_file', 'accesslog_file']

// The settings of a node: where its worker looks, and the defaults the site form starts from.
// A place can only be narrowed inside the root the worker's environment gives (the root is
// shown, read only); the worker tests the value before it is kept.
export function NodeSettings({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['nodeconfig', 'errors'])
  const client = useQueryClient()
  const { state } = useAuth()
  const needsCode = state.status === 'authenticated' && state.user.totp_enabled
  const base = `/nodes/${nodeId}/settings`
  const key = ['node-settings', nodeId]
  const settings = useQuery({ queryKey: key, queryFn: () => api.get<Settings>(base) })
  // What was typed, by name; a name missing here shows the saved value.
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [entrypoints, setEntrypoints] = useState<string | null>(null)
  const [resolver, setResolver] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [refused, setRefused] = useState<Record<string, string>>({})

  const save = useMutation({
    mutationFn: async () => {
      const places = Object.fromEntries(Object.entries(edits).filter(([name, v]) => v !== settings.data?.places[name]?.value))
      if (Object.keys(places).length > 0) {
        const checked = await api.post<{ errors: Record<string, string> }>(`${base}/check`, { places })
        if (Object.keys(checked.errors).length > 0) {
          setRefused(checked.errors)
          return false
        }
      }
      setRefused({})
      await api.put(base, {
        places,
        ...(entrypoints !== null && { traefik_entrypoints: entrypoints.split(/[\s,]+/).filter(Boolean) }),
        ...(resolver !== null && { traefik_resolver: resolver.trim() }),
        code,
      })
      return true
    },
    onSuccess: async (saved) => {
      if (!saved) return
      setEdits({})
      setEntrypoints(null)
      setResolver(null)
      setCode('')
      await client.invalidateQueries({ queryKey: key })
    },
  })

  if (settings.isPending) return <p>{t('loading')}</p>
  if (settings.isError) return <Problem error={settings.error} />
  const data = settings.data
  const hasTraefik = Object.keys(data.places).some((name) => TRAEFIK_PLACES.includes(name))
  const dirty = Object.keys(edits).length > 0 || entrypoints !== null || resolver !== null

  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate()
  }

  return (
    <form onSubmit={submit} aria-label={t('nodeconfig:title')}>
      <h2>{t('nodeconfig:title')}</h2>
      <p className="hint">{t('nodeconfig:explain')}</p>
      {data.error && <p className="error">{t(`errors:${data.error}`, { defaultValue: data.error })}</p>}
      <fieldset disabled={!data.can_write}>
        <legend>{t('nodeconfig:places')}</legend>
        {Object.entries(data.places).map(([name, place]) => (
          <label key={name}>
            {t(`nodeconfig:place.${name}`)}
            {place.root === null ? (
              <small className="hint">{t('nodeconfig:notSet')}</small>
            ) : (
              <span className="path-row">
                <code>{place.kind === 'file' ? `${place.root.replace(/[^/]*$/, '')}` : `${place.root}/`}</code>
                <input
                  value={edits[name] ?? place.value}
                  placeholder={place.kind === 'file' ? place.root.replace(/^.*\//, '') : t('nodeconfig:rootItself')}
                  onChange={(e) => setEdits({ ...edits, [name]: e.target.value })}
                />
              </span>
            )}
            {!place.valid && <small className="error">{t('nodeconfig:invalidNow')}</small>}
            {refused[name] && <small className="error">{t(`nodeconfig:reason.${refused[name]}`, { defaultValue: refused[name] })}</small>}
          </label>
        ))}
      </fieldset>
      {hasTraefik && (
        <fieldset disabled={!data.can_write}>
          <legend>{t('nodeconfig:traefik')}</legend>
          <label>
            {t('nodeconfig:entrypoints')}
            <input
              value={entrypoints ?? data.traefik_entrypoints.join(', ')}
              placeholder="websecure"
              onChange={(e) => setEntrypoints(e.target.value)}
            />
            <small className="hint">{t('nodeconfig:entrypointsHint')}</small>
          </label>
          <label>
            {t('nodeconfig:resolver')}
            <input value={resolver ?? data.traefik_resolver} placeholder="letsencrypt" onChange={(e) => setResolver(e.target.value)} />
          </label>
        </fieldset>
      )}
      {!data.can_write && <p className="hint">{t('nodeconfig:readOnly')}</p>}
      {data.can_write && (
        <>
          {needsCode && (
            <label>
              {t('nodeconfig:code')}
              <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" />
            </label>
          )}
          <div className="actions">
            <button type="submit" className="primary" disabled={!dirty || save.isPending}>
              {t('nodeconfig:save')}
            </button>
          </div>
        </>
      )}
      {save.isError && <Problem error={save.error} />}
    </form>
  )
}

import { Tip } from '../../components/Tip'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import { useAuth } from '../../core/auth'
import { TerminalView } from './TerminalView'

interface Settings {
  enabled: boolean
  writable: boolean
  network: boolean
  image: string
  can_manage: boolean
}

// The project's terminal: a shell in a throwaway container that sees this folder only. Turned
// off until someone allowed to switches it on; opening it asks for a fresh authenticator code
// when the account has one.
export function TerminalPanel({ nodeId, project }: { nodeId: string; project: string }) {
  const { t } = useTranslation(['terminal', 'errors'])
  const { state } = useAuth()
  const needsCode = state.status === 'authenticated' && state.user.totp_enabled
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/projects/${encodeURIComponent(project)}/terminal`
  const key = ['terminal-settings', nodeId, project]
  const settings = useQuery({ queryKey: key, queryFn: () => api.get<Settings>(base), retry: false })
  const [code, setCode] = useState('')
  const [ticket, setTicket] = useState<string | null>(null)
  const [draft, setDraft] = useState<Settings | null>(null)

  const open = useMutation({
    mutationFn: () => api.post<{ ticket: string }>(base, { code }),
    onSuccess: (r) => {
      setCode('')
      setTicket(r.ticket)
    },
  })
  const save = useMutation({
    mutationFn: (s: Settings) =>
      api.put(base, { enabled: s.enabled, writable: s.writable, network: s.network, image: s.image, code }),
    onSuccess: () => {
      setCode('')
      setDraft(null)
      return client.invalidateQueries({ queryKey: key })
    },
  })

  if (settings.isError || !settings.data) return null
  const current = draft ?? settings.data
  if (!settings.data.enabled && !settings.data.can_manage) return null
  const submit = (e: FormEvent) => {
    e.preventDefault()
    save.mutate(current)
  }
  const codeField = needsCode && (
    <label>
      {t('terminal:code')}
      <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" />
    </label>
  )
  return (
    <section className="panel" aria-label={t('terminal:title')}>
      <h3>{t('terminal:title')}</h3>
      <p className="hint">{t('terminal:explain')}</p>
      {ticket ? (
        <TerminalView ticket={ticket} onClose={() => setTicket(null)} />
      ) : (
        settings.data.enabled && (
          <div>
            {codeField}
            <div className="actions">
              <button type="button" className="primary" title={t('terminal:openTip')} onClick={() => open.mutate()} disabled={open.isPending}>
                {t('terminal:open')}
              </button>
            </div>
            {open.isError && <Problem error={open.error} />}
          </div>
        )
      )}
      {settings.data.can_manage && (
        <details>
          <summary>{t('terminal:settings')}</summary>
          <form onSubmit={submit}>
            <label className="legend-item">
              <input type="checkbox" checked={current.enabled} onChange={(e) => setDraft({ ...current, enabled: e.target.checked })} />{' '}
              {t('terminal:enabled')}
            </label>
            <label className="legend-item">
              <input type="checkbox" checked={current.writable} onChange={(e) => setDraft({ ...current, writable: e.target.checked })} />{' '}
              {t('terminal:writable')}
              <Tip text={t('terminal:writableTip')} />
            </label>
            <label className="legend-item">
              <input type="checkbox" checked={current.network} onChange={(e) => setDraft({ ...current, network: e.target.checked })} />{' '}
              {t('terminal:network')}
              <Tip text={t('terminal:networkTip')} />
            </label>
            <label>
              {t('terminal:image')}
              <input value={current.image} onChange={(e) => setDraft({ ...current, image: e.target.value })} />
            </label>
            {codeField}
            <div className="actions">
              <button type="submit" className="primary" disabled={save.isPending || draft === null}>
                {t('terminal:save')}
              </button>
            </div>
            {save.isError && <Problem error={save.error} />}
          </form>
        </details>
      )}
    </section>
  )
}

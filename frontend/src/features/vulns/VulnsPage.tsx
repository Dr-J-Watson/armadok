import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface Finding {
  id: number
  component: string
  version: string
  vuln_id: string
  severity: 'critical' | 'high' | 'moderate' | 'low' | 'unknown'
  summary: string
  fixed: string
  aliases: string[]
  first_seen: string
  ignored_reason: string | null
}

interface Answer {
  enabled: boolean
  scan: { finished_at: string | null; ok: boolean; error: string | null; components: number } | null
  findings: Finding[]
  can_manage: boolean
}

// Known weaknesses in the software Armadok itself runs on, from the public OSV database.
// Only package names and versions are sent; nothing is looked up unless the operator turned
// the check on.
export function VulnsPage() {
  const { t } = useTranslation(['vulns', 'errors'])
  const client = useQueryClient()
  const [accepting, setAccepting] = useState<Finding | null>(null)
  const [reason, setReason] = useState('')
  const list = useQuery({ queryKey: ['vulns'], queryFn: () => api.get<Answer>('/vulns') })
  const refresh = () => client.invalidateQueries({ queryKey: ['vulns'] })
  const scan = useMutation({ mutationFn: () => api.post<{ announced: number }>('/vulns/scan'), onSettled: refresh })
  const accept = useMutation({
    mutationFn: () => api.put(`/vulns/${accepting!.id}/ignore`, { reason }),
    onSuccess: () => {
      setAccepting(null)
      setReason('')
      return refresh()
    },
  })
  const revoke = useMutation({ mutationFn: (f: Finding) => api.del(`/vulns/${f.id}/ignore`), onSuccess: refresh })
  const failed = list.error ?? scan.error ?? accept.error ?? revoke.error
  const data = list.data

  function submit(event: FormEvent) {
    event.preventDefault()
    accept.mutate()
  }

  return (
    <main>
      <h1>{t('vulns:title')}</h1>
      <p>{t('vulns:explain')}</p>
      {failed != null && (
        <Problem error={failed} />
      )}
      {list.isPending && <p>{t('loading')}</p>}
      {data && !data.enabled && <p className="notice">{t('vulns:off')}</p>}
      {data?.enabled && (
        <p>
          {data.scan
            ? data.scan.ok
              ? t('vulns:lastOk', { at: data.scan.finished_at ? new Date(data.scan.finished_at).toLocaleString() : '', count: data.scan.components })
              : t('vulns:lastFailed', { code: data.scan.error ?? '' })
            : t('vulns:never')}{' '}
          {data.can_manage && (
            <button type="button" disabled={scan.isPending} onClick={() => scan.mutate()}>
              {t('vulns:scanNow')}
            </button>
          )}
          {scan.isSuccess && <span role="status"> {t('vulns:scanned', { count: scan.data.announced })}</span>}
        </p>
      )}
      {data?.enabled && data.scan?.ok && data.findings.length === 0 && <p className="ok-text">{t('vulns:none')}</p>}
      <ul className="links">
        {(data?.findings ?? []).map((f) => (
          <li key={f.id}>
            <span className={`badge ${f.severity === 'critical' || f.severity === 'high' ? 'danger-badge' : 'off'}`}>{t(`vulns:severity.${f.severity}`)}</span>{' '}
            <strong>
              {f.component} {f.version}
            </strong>{' '}
            — {f.summary} <small>({[f.vuln_id, ...f.aliases].join(', ')})</small>{' '}
            {f.fixed ? <span>{t('vulns:fixedIn', { version: f.fixed })}</span> : <span>{t('vulns:noFix')}</span>}
            {f.ignored_reason && (
              <span className="notice">
                {' '}
                {t('vulns:accepted', { reason: f.ignored_reason })}
              </span>
            )}
            {data?.can_manage &&
              (f.ignored_reason ? (
                <button type="button" onClick={() => revoke.mutate(f)}>
                  {t('vulns:stopAccepting')}
                </button>
              ) : (
                <button type="button" onClick={() => setAccepting(f)}>
                  {t('vulns:accept')}
                </button>
              ))}
          </li>
        ))}
      </ul>
      {accepting && (
        <form onSubmit={submit} aria-label={t('vulns:acceptTitle')} className="notice">
          <h2>{t('vulns:acceptTitle')}</h2>
          <p>
            {accepting.component} {accepting.version} — {accepting.vuln_id}
          </p>
          <label>
            {t('vulns:reason')}
            <input value={reason} required minLength={3} maxLength={300} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button type="submit" disabled={accept.isPending}>
            {t('vulns:accept')}
          </button>{' '}
          <button type="button" onClick={() => setAccepting(null)}>
            {t('vulns:cancel')}
          </button>
        </form>
      )}
    </main>
  )
}

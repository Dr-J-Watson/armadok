import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Finding {
  component: string
  version: string
  id: string
  severity: string
  summary: string
  fixed: string
}

interface Scan {
  image_id: string
  name: string
  os: string
  os_version: string
  packages: number
  scanned_at: string
  status: string
  counts: Record<string, number>
  findings: Finding[]
}

interface Answer {
  enabled: boolean
  scans: Scan[]
  can_run: boolean
}

const SEVERITIES = ['critical', 'high', 'unknown', 'moderate', 'low']

// Known flaws in the images of this machine's containers. The scan reads the package list of an
// image without starting it, then asks a public database about names and versions only.
export function ImageScanTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['imagescan', 'errors'])
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/imagescan`
  const list = useQuery({ queryKey: ['imagescan', nodeId], queryFn: () => api.get<Answer>(base) })
  const scan = useMutation({
    mutationFn: (image?: string) => api.post<{ results: { name: string; status: string }[] }>(`${base}/scan`, image ? { image } : {}),
    onSuccess: () => client.invalidateQueries({ queryKey: ['imagescan', nodeId] }),
  })
  const failed = list.error ?? scan.error
  const data = list.data

  return (
    <>
      <p>{t('imagescan:explain')}</p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      {data && !data.enabled && <p className="notice">{t('imagescan:off')}</p>}
      {data?.can_run && data.enabled && (
        <button type="button" disabled={scan.isPending} onClick={() => scan.mutate(undefined)}>
          {scan.isPending ? t('imagescan:scanning') : t('imagescan:scanAll')}
        </button>
      )}
      {data && data.scans.length === 0 && <p className="empty">{t('imagescan:none')}</p>}
      <ul className="links">
        {data?.scans.map((s) => (
          <li key={s.image_id}>
            <strong>{s.name}</strong> — {s.os ? `${s.os} ${s.os_version}` : t('imagescan:unknownOs')} — {t('imagescan:packages', { count: s.packages })} —{' '}
            {new Date(s.scanned_at).toLocaleString()}
            {s.status === 'ok' ? (
              <span>
                {' '}
                {SEVERITIES.filter((k) => s.counts[k]).map((k) => (
                  <span key={k} className={`badge ${k === 'critical' || k === 'high' ? 'off' : ''}`}>
                    {t(`imagescan:severity.${k}`)} {s.counts[k]}{' '}
                  </span>
                ))}
                {Object.keys(s.counts).length === 0 && <span className="badge ok">{t('imagescan:clean')}</span>}
              </span>
            ) : (
              <span className="notice"> {t(`imagescan:status.${s.status}`, { defaultValue: t(`errors:${s.status}`, { defaultValue: s.status }) })}</span>
            )}
            {data.can_run && data.enabled && (
              <>
                {' '}
                <button type="button" disabled={scan.isPending} onClick={() => scan.mutate(s.image_id)}>
                  {t('imagescan:rescan')}
                </button>
              </>
            )}
            {s.findings.length > 0 && (
              <details>
                <summary>{t('imagescan:details', { count: s.findings.length })}</summary>
                <ul>
                  {s.findings.map((f) => (
                    <li key={`${f.component}:${f.id}`}>
                      <code>{f.id}</code> {f.component} {f.version} — {t(`imagescan:severity.${f.severity}`, { defaultValue: f.severity })}
                      {f.fixed && <span> — {t('imagescan:fixedIn', { version: f.fixed })}</span>}
                      <br />
                      <small>{f.summary}</small>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}

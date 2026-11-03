import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface WorkerStatus {
  enrolled: boolean
  worker_id: string | null
  connected: boolean
  last_seen_at: string | null
}

interface Enrolment {
  token: string
  expires_at: string
  hub_key: string
}

// Where the worker must dial: the address this page was loaded from, as ws(s)://.
function hubUrl(): string {
  return `${window.location.origin.replace(/^http/, 'ws')}/api/v1/worker/connect`
}

export function WorkerTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['workers', 'errors'])
  const queryClient = useQueryClient()
  const base = `/nodes/${nodeId}/worker`
  const status = useQuery({
    queryKey: ['worker', nodeId],
    queryFn: () => api.get<WorkerStatus>(base),
    refetchInterval: 5000,
  })
  // The token is shown once and kept only in this component's memory.
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null)
  const [copied, setCopied] = useState(false)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['worker', nodeId] })
  const enrol = useMutation({
    mutationFn: () => api.post<Enrolment>(`${base}/enrol`),
    onSuccess: (data) => setEnrolment(data),
  })
  const revoke = useMutation({
    mutationFn: () => api.del(base),
    onSuccess: () => {
      setEnrolment(null)
      return refresh()
    },
  })
  const ping = useMutation({ mutationFn: () => api.post<{ pong: boolean }>(`${base}/ping`) })

  const snippet = enrolment
    ? `ARMADOK_HUB_URL=${hubUrl()}\nARMADOK_HUB_KEY=${enrolment.hub_key}\nARMADOK_ENROL_TOKEN=${enrolment.token}`
    : ''

  async function copy() {
    try {
      await navigator.clipboard.writeText(snippet)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const failed = status.error ?? enrol.error ?? revoke.error ?? ping.error

  return (
    <>
      {status.data && (
        <p>
          <span className={`badge ${status.data.connected ? 'ok' : 'off'}`}>
            {status.data.connected
              ? t('workers:state.connected')
              : status.data.enrolled
                ? t('workers:state.offline')
                : t('workers:state.none')}
          </span>
          {status.data.last_seen_at && (
            <>
              {' '}
              {t('workers:lastSeen', { date: new Date(status.data.last_seen_at).toLocaleString() })}
            </>
          )}
        </p>
      )}
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}

      <div className="actions">
        <button type="button" className="primary" onClick={() => enrol.mutate()}>
          {status.data?.enrolled ? t('workers:enrolAgain') : t('workers:enrol')}
        </button>
        {status.data?.connected && (
          <button type="button" onClick={() => ping.mutate()}>
            {t('workers:ping')}
          </button>
        )}
        {status.data?.enrolled && (
          <button type="button" onClick={() => revoke.mutate()}>
            {t('workers:revoke')}
          </button>
        )}
      </div>
      {ping.data && <p className="ok-text">{t('workers:pingOk')}</p>}

      {enrolment && (
        <section className="notice">
          <h2>{t('workers:enrolment.title')}</h2>
          <p>{t('workers:enrolment.explain', { time: new Date(enrolment.expires_at).toLocaleTimeString() })}</p>
          <pre>{snippet}</pre>
          <button type="button" onClick={() => void copy()}>
            {copied ? t('workers:enrolment.copied') : t('workers:enrolment.copy')}
          </button>
          <p className="hint">{t('workers:enrolment.once')}</p>
        </section>
      )}
    </>
  )
}

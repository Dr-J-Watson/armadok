import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { codeMessage, errorMessage } from '../../core/errors'

interface Item {
  ref: string
  state: 'current' | 'update' | 'unknown' | 'error'
  in_use: boolean
  remote_digest: string | null
  error: string | null
  checked_at: string
}

const BADGE: Record<Item['state'], string> = { current: 'ok', update: 'danger-badge', unknown: 'off', error: 'off' }

// Is a newer version of the images the containers run out? Found by comparing the digest of
// each tag with the registry's, so nothing is downloaded. Updating itself is a redeploy of the
// project with "pull", which the schedule tab can do on its own.
export function ImageUpdatesTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['imageupdates', 'errors'])
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/image-updates`
  const key = ['image-updates', nodeId]
  const list = useQuery({ queryKey: key, queryFn: () => api.get<{ images: Item[]; can_check: boolean }>(base) })
  const check = useMutation({
    mutationFn: () => api.post<{ counts: Record<string, number> }>(`${base}/check`),
    onSuccess: () => client.invalidateQueries({ queryKey: key }),
  })
  const failed = list.error ?? check.error
  return (
    <section aria-label={t('imageupdates:title')}>
      <h2>{t('imageupdates:title')}</h2>
      <p className="hint">{t('imageupdates:explain')}</p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      {list.data?.can_check && (
        <div className="actions">
          <button type="button" className="primary" disabled={check.isPending} onClick={() => check.mutate()}>
            {t('imageupdates:check')}
          </button>
        </div>
      )}
      {list.data && list.data.images.length === 0 && <p className="empty">{t('imageupdates:none')}</p>}
      <ul className="links">
        {(list.data?.images ?? []).map((i) => (
          <li key={i.ref}>
            <strong>{i.ref}</strong>{' '}
            <span className={`badge ${BADGE[i.state]}`}>{t(`imageupdates:state.${i.state}`)}</span>
            {i.error && <small> — {codeMessage(t, i.error)}</small>}
            <small> — {t('imageupdates:checked', { at: new Date(i.checked_at).toLocaleString() })}</small>
          </li>
        ))}
      </ul>
    </section>
  )
}

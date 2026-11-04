import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Page {
  published: boolean
  slug: string | null
  title: string | null
}

// Open or close the public page of one probe. The address is the only protection of the page,
// so it is shown to the people who manage the probe and nowhere else.
export function PublishControl({ nodeId, probeId, defaultTitle }: { nodeId: string; probeId: string; defaultTitle: string }) {
  const { t } = useTranslation(['status', 'errors'])
  const client = useQueryClient()
  const path = `/nodes/${nodeId}/uptime/${probeId}/public`
  const key = ['status-page', nodeId, probeId]
  const page = useQuery({ queryKey: key, queryFn: () => api.get<Page>(path) })
  const [title, setTitle] = useState(defaultTitle)
  const refresh = () => client.invalidateQueries({ queryKey: key })
  const open = useMutation({ mutationFn: () => api.put(path, { title }), onSuccess: refresh })
  const close = useMutation({ mutationFn: () => api.del(path), onSuccess: refresh })
  const failed = page.error ?? open.error ?? close.error
  const link = page.data?.slug ? `${window.location.origin}/status/${page.data.slug}` : null
  return (
    <details>
      <summary>{page.data?.published ? t('status:published') : t('status:publish')}</summary>
      <p className="hint">{t('status:hint')}</p>
      <label>
        {t('status:title')}
        <input value={title} maxLength={80} onChange={(e) => setTitle(e.target.value)} />
      </label>{' '}
      <button type="button" disabled={open.isPending || title.trim() === ''} onClick={() => open.mutate()}>
        {page.data?.published ? t('status:retitle') : t('status:open')}
      </button>
      {page.data?.published && (
        <>
          {' '}
          <button type="button" onClick={() => close.mutate()}>
            {t('status:close')}
          </button>
          {link && (
            <p>
              {t('status:address')} <a href={link}>{link}</a>
            </p>
          )}
        </>
      )}
      {failed && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
    </details>
  )
}

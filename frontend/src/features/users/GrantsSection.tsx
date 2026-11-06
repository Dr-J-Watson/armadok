import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { useCan } from '../../core/auth'
import type { RightInfo } from '../account/rights'
import type { Node } from '../nodes/types'
import { Problem } from '../../components/Problem'
import { useConfirm } from '../../components/Confirm'
import { Tip } from '../../components/Tip'

interface StoredGrant {
  id: string
  right: string
  scope: string
  expires_at: string | null
  granted_by: string
}

// Rights of one account: what it holds, and (with `grants.manage`) giving or removing them.
export function GrantsSection({ userId }: { userId: string }) {
  const { t } = useTranslation(['users', 'errors'])
  const queryClient = useQueryClient()
  const ask = useConfirm()
  const canManage = useCan('grants.manage')
  const base = `/users/${userId}/grants`
  const grants = useQuery({ queryKey: ['grants', userId], queryFn: () => api.get<StoredGrant[]>(base) })
  const catalogue = useQuery({ queryKey: ['rights'], queryFn: () => api.get<RightInfo[]>('/rights') })
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })

  const [right, setRight] = useState('')
  const [scope, setScope] = useState('*')
  const [expires, setExpires] = useState('')

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['grants', userId] })
  const add = useMutation({
    mutationFn: () =>
      api.post(base, { right, scope, expires_at: expires ? new Date(expires).toISOString() : null }),
    onSuccess: () => {
      setExpires('')
      return refresh()
    },
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`${base}/${id}`), onSuccess: refresh })

  const sensitive = new Set((catalogue.data ?? []).filter((r) => r.sensitive).map((r) => r.id))
  const nodeName = (scopeValue: string) => {
    const id = scopeValue.startsWith('node:') ? scopeValue.slice(5) : null
    return id ? (nodes.data?.find((n) => n.id === id)?.name ?? scopeValue) : t('users:scope.everywhere')
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    add.mutate()
  }
  const failed = grants.error ?? add.error ?? remove.error

  return (
    <section>
      <h2>
        {t('users:grants.title')}
        <Tip text={t('users:grants.tip')} />
      </h2>
      {failed != null && (
        <Problem error={failed} />
      )}
      {grants.data?.length === 0 && <p className="empty">{t('users:grants.empty')}</p>}
      <ul className="links">
        {grants.data?.map((grant) => (
          <li key={grant.id}>
            <code>{grant.right}</code>
            {sensitive.has(grant.right) && <span className="badge danger-badge">{t('users:grants.sensitive')}</span>}{' '}
            <span className="muted">
              · {nodeName(grant.scope)}
              {grant.expires_at && <> · {t('users:grants.until', { date: new Date(grant.expires_at).toLocaleString() })}</>}
            </span>{' '}
            {canManage && (
              <button
                type="button"
                className="link-button"
                onClick={async () => {
                  if (await ask(t('users:grants.removeConfirm', { right: grant.right }))) remove.mutate(grant.id)
                }}
              >
                {t('users:grants.remove')}
              </button>
            )}
          </li>
        ))}
      </ul>

      {canManage && (
        <form className="form" onSubmit={submit}>
          <h3>{t('users:grants.addTitle')}</h3>
          <p className="hint">{t('users:grants.addExplain')}</p>
          <label htmlFor="grant-right">{t('users:grants.right')}</label>
          <select id="grant-right" required value={right} onChange={(e) => setRight(e.target.value)}>
            <option value="" disabled>
              {t('users:grants.choose')}
            </option>
            {catalogue.data?.map((r) => (
              <option key={r.id} value={r.id}>
                {r.id}
                {r.sensitive ? ` (${t('users:grants.sensitive')})` : ''}
              </option>
            ))}
          </select>
          <label htmlFor="grant-scope">
            {t('users:grants.scope')}
            <Tip text={t('users:grants.scopeTip')} />
          </label>
          <select id="grant-scope" value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="*">{t('users:scope.everywhere')}</option>
            {nodes.data?.map((n) => (
              <option key={n.id} value={`node:${n.id}`}>
                {t('users:scope.node', { name: n.name })}
              </option>
            ))}
          </select>
          <label htmlFor="grant-expires">
            {t('users:grants.expires')}
            <Tip text={t('users:grants.expiresTip')} />
          </label>
          <input id="grant-expires" type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
          <button type="submit" className="primary" disabled={add.isPending || !right}>
            {t('users:grants.add')}
          </button>
        </form>
      )}
    </section>
  )
}

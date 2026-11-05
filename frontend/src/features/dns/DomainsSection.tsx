import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import type { Node } from '../nodes/types'
import { Problem } from '../../components/Problem'

export interface Domain {
  id: string
  name: string
  node_id: string | null
  access: 'owner' | 'write' | 'read'
  capabilities: string[]
  provider: string | null
  account_name: string | null
  verified?: boolean
  member_types?: string[] | null
  member_apex?: boolean | null
  member_names?: string[] | null
}

interface Account {
  id: string
  name: string
  provider: string
}

export function DomainsSection() {
  const { t } = useTranslation(['dns', 'errors'])
  const queryClient = useQueryClient()
  const domains = useQuery({ queryKey: ['dns-domains'], queryFn: () => api.get<Domain[]>('/dns/domains') })
  const accounts = useQuery({ queryKey: ['dns-accounts'], queryFn: () => api.get<Account[]>('/dns/accounts') })
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const [name, setName] = useState('')
  const [accountId, setAccountId] = useState('')
  const [imported, setImported] = useState<string | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['dns-domains'] })
  const add = useMutation({
    mutationFn: () => api.post('/dns/domains', { name, account_id: accountId || null }),
    onSuccess: () => {
      setName('')
      return refresh()
    },
  })
  // Members of the node use YOUR provider account: they get a narrow default and the owner
  // can widen it. `limits` holds the form for the domain being attached.
  const [limits, setLimits] = useState<{ id: string; nodeId: string; types: Set<string>; apex: boolean; names: string } | null>(null)
  const attach = useMutation({
    mutationFn: (payload: { id: string; node_id: string | null; member_types?: string[]; member_apex?: boolean; member_names?: string[] }) =>
      api.patch(`/dns/domains/${payload.id}`, payload),
    onSuccess: () => {
      setLimits(null)
      return refresh()
    },
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`/dns/domains/${id}`), onSuccess: refresh })
  const importZones = useMutation({
    mutationFn: async (account: string) => {
      const zones = await api.get<string[]>(`/dns/accounts/${account}/zones`)
      return api.post<{ added: string[] }>(`/dns/accounts/${account}/import`, { zones })
    },
    onSuccess: (result) => {
      setImported(result.added.length ? result.added.join(', ') : '')
      return refresh()
    },
  })
  const failed = domains.error ?? add.error ?? attach.error ?? remove.error ?? importZones.error

  function submit(event: FormEvent) {
    event.preventDefault()
    add.mutate()
  }

  return (
    <section>
      <h2>{t('dns:domains.title')}</h2>
      {failed != null && <Problem error={failed} />}
      {domains.data?.length === 0 && <p className="empty">{t('dns:domains.empty')}</p>}
      <ul className="cards">
        {domains.data?.map((d) => (
          <li key={d.id}>
            <Link to={`/dns/${d.id}`}><strong>{d.name}</strong></Link>
            <span className="badge">{d.provider ? t(`dns:providers.${d.provider}`, { defaultValue: d.provider }) : t('dns:domains.shared')}</span>
            {d.access !== 'owner' && <span className="badge">{t(`dns:access.${d.access}`)}</span>}
            {d.verified === false && <span className="badge off" title={t('dns:domains.unverifiedHint')}>{t('dns:domains.unverified')}</span>}
            {d.access === 'owner' && (
              <>
                <p>
                  <label>
                    <span className="visually-hidden">{t('dns:domains.attached')}</span>
                    <select
                      aria-label={`${d.name} — ${t('dns:domains.attached')}`}
                      value={d.node_id ?? ''}
                      onChange={(e) =>
                        e.target.value
                          ? setLimits({ id: d.id, nodeId: e.target.value, types: new Set(d.member_types ?? ['A', 'AAAA', 'CNAME']), apex: d.member_apex ?? false, names: (d.member_names ?? []).join(', ') })
                          : attach.mutate({ id: d.id, node_id: null })
                      }
                    >
                      <option value="">{t('dns:domains.notAttached')}</option>
                      {nodes.data?.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
                    </select>
                  </label>
                </p>
                <button type="button" onClick={() => remove.mutate(d.id)}>{t('dns:domains.remove')}</button>
              </>
            )}
          </li>
        ))}
      </ul>

      {limits && (
        <form
          className="form notice"
          onSubmit={(e) => {
            e.preventDefault()
            attach.mutate({
              id: limits.id,
              node_id: limits.nodeId,
              member_types: [...limits.types],
              member_apex: limits.apex,
              member_names: limits.names.split(',').map((n) => n.trim()).filter(Boolean),
            })
          }}
        >
          <h3>{t('dns:attach.title')}</h3>
          <p role="note">{t('dns:attach.warning')}</p>
          <fieldset className="legend">
            <legend>{t('dns:attach.types')}</legend>
            {['A', 'AAAA', 'CNAME', 'TXT', 'MX', 'NS', 'SRV', 'CAA'].map((type) => (
              <label key={type} className="legend-item">
                <input
                  type="checkbox"
                  checked={limits.types.has(type)}
                  onChange={() => {
                    const next = new Set(limits.types)
                    if (next.has(type)) next.delete(type)
                    else next.add(type)
                    setLimits({ ...limits, types: next })
                  }}
                />
                {type}
              </label>
            ))}
          </fieldset>
          <label className="legend-item">
            <input type="checkbox" checked={limits.apex} onChange={(e) => setLimits({ ...limits, apex: e.target.checked })} />
            {t('dns:attach.apex')}
          </label>
          <label htmlFor="attach-names">{t('dns:attach.names')}</label>
          <input id="attach-names" value={limits.names} placeholder="dev, staging" onChange={(e) => setLimits({ ...limits, names: e.target.value })} />
          <p className="hint">{t('dns:attach.namesHint')}</p>
          <button type="submit" className="primary" disabled={attach.isPending}>{t('dns:attach.confirm')}</button>{' '}
          <button type="button" onClick={() => setLimits(null)}>{t('dns:attach.cancel')}</button>
        </form>
      )}

      {accounts.data?.map((a) => (
        <p key={a.id}>
          <button type="button" onClick={() => importZones.mutate(a.id)}>{t('dns:domains.import', { account: a.name })}</button>
        </p>
      ))}
      {imported !== null && <p role="status" className="ok-text">{imported ? t('dns:domains.imported', { names: imported }) : t('dns:domains.nothingNew')}</p>}

      <form className="form" onSubmit={submit}>
        <h3>{t('dns:domains.add')}</h3>
        <label htmlFor="dns-domain-name">{t('dns:domains.name')}</label>
        <input id="dns-domain-name" required placeholder="example.com" value={name} onChange={(e) => setName(e.target.value)} />
        <label htmlFor="dns-domain-account">{t('dns:domains.account')}</label>
        <select id="dns-domain-account" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
          <option value="">{t('dns:domains.local')}</option>
          {accounts.data?.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <p className="hint">{t('dns:domains.localHint')}</p>
        <button type="submit" className="primary" disabled={add.isPending}>{t('dns:domains.create')}</button>
      </form>
    </section>
  )
}

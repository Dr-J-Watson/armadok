import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { api } from '../../core/api'
import type { Domain } from './DomainsSection'
import { Problem } from '../../components/Problem'

interface DnsRecord {
  id: string
  type: string
  name: string
  target: string
  ttl: number
}

const TYPES = ['A', 'AAAA', 'CNAME', 'TXT', 'MX', 'NS', 'SRV', 'CAA']

export function DomainPage() {
  const { t } = useTranslation(['dns', 'errors'])
  const { domainId = '' } = useParams()
  const queryClient = useQueryClient()
  const domains = useQuery({ queryKey: ['dns-domains'], queryFn: () => api.get<Domain[]>('/dns/domains') })
  const records = useQuery({
    queryKey: ['dns-records', domainId],
    queryFn: () => api.get<{ records: DnsRecord[]; capabilities: string[] }>(`/dns/domains/${domainId}/records`),
  })
  const [editing, setEditing] = useState<DnsRecord | null>(null)
  const [type, setType] = useState('A')
  const [name, setName] = useState('')
  const [target, setTarget] = useState('')
  const [ttl, setTtl] = useState(3600)
  const [search, setSearch] = useState('')

  const domain = domains.data?.find((d) => d.id === domainId)
  const caps = records.data?.capabilities ?? domain?.capabilities ?? []
  const canWrite = domain?.access !== 'read' && (caps.includes('write') || caps.includes('publish_ip'))
  const base = `/dns/domains/${domainId}/records`
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['dns-records', domainId] })
  const reset = () => { setEditing(null); setName(''); setTarget(''); setTtl(3600); setType('A') }

  const save = useMutation({
    mutationFn: () => {
      const body = { type, name, target, ttl }
      return editing ? api.put(`${base}/${encodeURIComponent(editing.id)}`, body) : api.post(base, body)
    },
    onSuccess: () => { reset(); return refresh() },
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`${base}/${encodeURIComponent(id)}`), onSuccess: refresh })

  if (domains.isPending) return <p>{t('loading')}</p>
  if (!domain) return <><p role="alert" className="error">{t('errors:common.not_found')}</p><Link to="/dns">{t('dns:back')}</Link></>

  const shown = (records.data?.records ?? []).filter((r) => `${r.name} ${r.target} ${r.type}`.toLowerCase().includes(search.toLowerCase()))
  const failed = records.error ?? save.error ?? remove.error

  function edit(r: DnsRecord) {
    setEditing(r); setType(r.type); setName(r.name); setTarget(r.target); setTtl(r.ttl)
  }
  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate()
  }

  return (
    <>
      <p><Link to="/dns">{t('dns:back')}</Link></p>
      <h1>{domain.name}</h1>
      {failed != null && <Problem error={failed} />}
      {caps.length === 0 && <p className="notice">{t('dns:records.localExplain')}</p>}
      {caps.includes('publish_ip') && !caps.includes('write') && <p className="notice">{t('dns:records.publishIpExplain')}</p>}

      {caps.includes('records') && (
        <>
          <label className="visually-hidden" htmlFor="dns-search">{t('dns:records.search')}</label>
          <input id="dns-search" type="search" placeholder={t('dns:records.search')} value={search} onChange={(e) => setSearch(e.target.value)} />
          {shown.length === 0 && <p className="empty">{t('dns:records.empty')}</p>}
          {shown.length > 0 && (
            <table className="table">
              <thead>
                <tr>
                  <th>{t('dns:records.type')}</th><th>{t('dns:records.name')}</th><th>{t('dns:records.target')}</th><th>{t('dns:records.ttl')}</th><th />
                </tr>
              </thead>
              <tbody>
                {shown.map((r) => (
                  <tr key={r.id}>
                    <td data-label={t('dns:records.type')}>{r.type}</td>
                    <td data-label={t('dns:records.name')}>{r.name || '@'}</td>
                    <td data-label={t('dns:records.target')}><code>{r.target}</code></td>
                    <td data-label={t('dns:records.ttl')}>{r.ttl}</td>
                    <td>
                      {canWrite && caps.includes('write') && (
                        <>
                          <button type="button" className="link-button" onClick={() => edit(r)}>{t('dns:records.edit')}</button>{' '}
                          <button type="button" className="link-button" onClick={() => remove.mutate(r.id)}>{t('dns:records.delete')}</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      {canWrite && (
        <form className="form" onSubmit={submit}>
          <h2>{editing ? t('dns:records.editTitle') : t('dns:records.addTitle')}</h2>
          <label htmlFor="rec-type">{t('dns:records.type')}</label>
          <select id="rec-type" value={type} disabled={editing !== null} onChange={(e) => setType(e.target.value)}>
            {TYPES.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
          <label htmlFor="rec-name">{t('dns:records.nameHelp')}</label>
          <input id="rec-name" value={name} onChange={(e) => setName(e.target.value)} />
          <label htmlFor="rec-target">{t('dns:records.target')}</label>
          <input id="rec-target" required value={target} onChange={(e) => setTarget(e.target.value)} />
          <label htmlFor="rec-ttl">{t('dns:records.ttl')}</label>
          <input id="rec-ttl" type="number" min={60} max={604800} value={ttl} onChange={(e) => setTtl(Number(e.target.value))} />
          <button type="submit" className="primary" disabled={save.isPending}>{editing ? t('dns:records.save') : t('dns:records.add')}</button>
          {editing && <button type="button" onClick={reset}>{t('dns:records.cancel')}</button>}
        </form>
      )}
    </>
  )
}

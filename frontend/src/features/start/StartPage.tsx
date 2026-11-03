import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { useCan } from '../../core/auth'
import { readPref, writePref } from '../../core/storage'
import type { Node } from '../nodes/types'

export const DISMISSED = 'start-dismissed'

type Level = 'ok' | 'todo' | 'warn'
interface Row {
  id: string
  level: Level
  text: string
  to?: string
}

interface Status {
  version: string
  https: boolean
  cookie_secure: boolean
  encryption_key_set: boolean
  proxy_trust: 'none' | 'restricted' | 'any'
}

const CAPABILITIES = ['docker', 'projects', 'traefik', 'certificates'] as const

function Badge({ level }: { level: Level }) {
  const { t } = useTranslation(['start'])
  return <span className={`badge ${level === 'ok' ? 'ok' : 'off'}`}>{t(`start:level.${level}`)}</span>
}

// What the machine can do, asked of its worker. One line per capability.
function Machine({ node }: { node: Node }) {
  const { t } = useTranslation(['start'])
  const ready = useQuery({
    queryKey: ['readiness', node.id],
    queryFn: () => api.get<{ checks: Record<string, string> }>(`/nodes/${node.id}/readiness`),
    retry: false,
    // Asking a machine's worker is not free: no new check on every focus.
    staleTime: 30000,
    refetchOnWindowFocus: false,
  })
  return (
    <li>
      <strong>{node.name}</strong> <small>({node.type})</small>
      {ready.isPending && <small> …</small>}
      {ready.isError && <small> — {t('start:machine.unreachable')}</small>}
      {ready.data && (
        <ul className="links">
          {CAPABILITIES.map((c) => {
            const state = ready.data.checks[c]
            if (state === 'not_applicable' || state === 'no_right') return null
            return (
              <li key={c}>
                <Badge level={state === 'ok' ? 'ok' : state === 'not_configured' ? 'todo' : 'warn'} />{' '}
                {t(`start:capability.${c}`)}: {t(`start:state.${state}`, { defaultValue: state })}
              </li>
            )
          })}
        </ul>
      )}
    </li>
  )
}

// The first-start checklist. It reads the real state of the system, changes nothing, and each
// line that is not done says where to do it. It can be hidden once you are satisfied.
export function StartPage() {
  const { t } = useTranslation(['start', 'errors'])
  const admin = useCan('users.create')
  const [hidden, setHidden] = useState(readPref(DISMISSED) === '1')
  const me = useQuery({ queryKey: ['me'], queryFn: () => api.get<{ totp_enabled: boolean }>('/auth/me') })
  const status = useQuery({ queryKey: ['system-status'], queryFn: () => api.get<Status>('/system/status'), retry: false })
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const dns = useQuery({ queryKey: ['dns-accounts'], queryFn: () => api.get<unknown[]>('/dns/accounts'), retry: false })
  const channels = useQuery({ queryKey: ['channels'], queryFn: () => api.get<unknown[]>('/notifications/channels'), retry: false })

  if (!admin) return <p className="hint">{t('start:notAdmin')}</p>

  const rows: Row[] = []
  if (me.data)
    rows.push({ id: 'twofa', level: me.data.totp_enabled ? 'ok' : 'todo', text: t(me.data.totp_enabled ? 'start:twofa.on' : 'start:twofa.off'), to: '/account' })
  if (status.data) {
    const s = status.data
    rows.push({ id: 'https', level: s.https ? 'ok' : 'warn', text: t(s.https ? 'start:https.on' : 'start:https.off') })
    rows.push({ id: 'cookie', level: s.cookie_secure ? 'ok' : 'warn', text: t(s.cookie_secure ? 'start:cookie.on' : 'start:cookie.off') })
    rows.push({ id: 'key', level: s.encryption_key_set ? 'ok' : 'warn', text: t(s.encryption_key_set ? 'start:key.on' : 'start:key.off') })
    rows.push({ id: 'proxy', level: s.proxy_trust === 'restricted' ? 'ok' : s.proxy_trust === 'any' ? 'warn' : 'todo', text: t(`start:proxy.${s.proxy_trust}`) })
  }
  if (dns.data) rows.push({ id: 'dns', level: dns.data.length > 0 ? 'ok' : 'todo', text: t(dns.data.length > 0 ? 'start:dns.on' : 'start:dns.off'), to: '/dns' })
  if (channels.data)
    rows.push({ id: 'notify', level: channels.data.length > 0 ? 'ok' : 'todo', text: t(channels.data.length > 0 ? 'start:notify.on' : 'start:notify.off'), to: '/account' })
  const open = rows.filter((r) => r.level !== 'ok').length

  function hide() {
    writePref(DISMISSED, '1')
    setHidden(true)
  }

  return (
    <>
      <h1>{t('start:title')}</h1>
      <p>{t('start:intro')}</p>
      <p role="status" className={open === 0 ? 'notice' : 'hint'}>
        {open === 0 ? t('start:allGood') : t('start:toLookAt', { count: open })}
      </p>
      <section>
        <h2>{t('start:machines')}</h2>
        {nodes.data?.length === 0 && <p className="empty">{t('start:noMachine')}</p>}
        <ul className="links">
          {nodes.data?.map((n) => (
            <Machine key={n.id} node={n} />
          ))}
        </ul>
        <Link to="/nodes">{t('start:manageMachines')}</Link>
      </section>
      <section>
        <h2>{t('start:checks')}</h2>
        <ul className="links">
          {rows.map((r) => (
            <li key={r.id}>
              <Badge level={r.level} /> {r.text} {r.to && r.level !== 'ok' && <Link to={r.to}>{t('start:go')}</Link>}
            </li>
          ))}
        </ul>
      </section>
      <p className="hint">{t('start:envNote')}</p>
      {!hidden && (
        <button type="button" onClick={hide}>
          {t('start:hide')}
        </button>
      )}
    </>
  )
}

import { useQueries, useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowUpCircle, CheckCircle2, Server } from 'lucide-react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { NodeIcon } from '../../components/NodeIcon'
import { api } from '../../core/api'
import { useAuth, useCanFn } from '../../core/auth'
import type { Node } from '../nodes/types'

interface WorkerStatus {
  connected: boolean
  enrolled: boolean
}
interface Probe {
  name: string
  state: string
  enabled: boolean
}
interface ImageUpdate {
  ref: string
  state: string
}
interface AuditEntry {
  id: number
  at: string
  actor_name: string
  event: string
  outcome: string
}

const MAX_MACHINES = 12 // the overview looks at a dozen machines at most: it must stay light

// Where to start: how many machines, what needs a look, and what happened lately. Everything is
// read through the same API as the rest of the interface, so a person only sees what they may.
// A figure that cannot be read (no right, a machine that is off) is simply left out.
export function DashboardPage() {
  const { t } = useTranslation(['dashboard', 'audit', 'errors'])
  const { state } = useAuth()
  const can = useCanFn()
  const name = state.status === 'authenticated' ? state.user.username : ''
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const list = (nodes.data ?? []).filter((n) => !n.suspended).slice(0, MAX_MACHINES)
  const worker = useQueries({
    queries: list.map((n) => ({
      queryKey: ['dashboard', 'worker', n.id],
      queryFn: () => api.get<WorkerStatus>(`/nodes/${n.id}/worker`),
      retry: false,
      refetchInterval: 30_000,
    })),
  })
  const probes = useQueries({
    queries: list.map((n) => ({
      queryKey: ['dashboard', 'probes', n.id],
      queryFn: () => api.get<{ probes: Probe[] }>(`/nodes/${n.id}/uptime`),
      retry: false,
      refetchInterval: 60_000,
      enabled: n.type !== 'docker',
    })),
  })
  const updates = useQueries({
    queries: list.map((n) => ({
      queryKey: ['dashboard', 'updates', n.id],
      queryFn: () => api.get<{ images: ImageUpdate[] }>(`/nodes/${n.id}/image-updates`),
      retry: false,
      enabled: n.type !== 'network',
    })),
  })
  const unread = useQuery({
    queryKey: ['notifications', 'unread'],
    queryFn: () => api.get<{ unread: number }>('/notifications/unread-count'),
    retry: false,
  })
  const activity = useQuery({
    queryKey: ['dashboard', 'activity'],
    queryFn: () => api.get<{ entries: AuditEntry[] }>('/audit?limit=6'),
    enabled: can('audit.read'),
    retry: false,
  })

  const attention: { key: string; icon: ReactNode; text: string; to: string }[] = []
  let connected = 0
  let down = 0
  let outdated = 0
  list.forEach((n, i) => {
    const w = worker[i]?.data
    if (w?.connected) connected += 1
    else if (w && !n.local)
      attention.push({
        key: `w-${n.id}`,
        icon: <AlertTriangle size={18} aria-hidden="true" />,
        text: t('dashboard:attention.worker', { machine: n.name }),
        to: `/nodes/${n.id}?tab=worker`,
      })
    for (const p of probes[i]?.data?.probes ?? []) {
      if (p.enabled && p.state === 'down') {
        down += 1
        attention.push({
          key: `p-${n.id}-${p.name}`,
          icon: <AlertTriangle size={18} aria-hidden="true" />,
          text: t('dashboard:attention.down', { site: p.name, machine: n.name }),
          to: `/nodes/${n.id}?tab=uptime`,
        })
      }
    }
    const old = (updates[i]?.data?.images ?? []).filter((u) => u.state === 'update')
    if (old.length > 0) {
      outdated += old.length
      attention.push({
        key: `u-${n.id}`,
        icon: <ArrowUpCircle size={18} aria-hidden="true" />,
        text: t('dashboard:attention.updates', { count: old.length, machine: n.name }),
        to: `/nodes/${n.id}?tab=image-updates`,
      })
    }
  })

  return (
    <>
      <header className="page-head page-head-split">
        <div>
          <h1>{t('dashboard:hello', { name })}</h1>
          <p className="hint">{t('dashboard:subtitle')}</p>
        </div>
      </header>

      <div className="stat-grid">
        <Link to="/nodes" className="stat stat-link">
          <span className="stat-label">{t('dashboard:stat.machines')}</span>
          <strong className="stat-value">{nodes.data?.length ?? '—'}</strong>
          <span className="hint">{t('dashboard:stat.connected', { count: connected })}</span>
        </Link>
        <div className={`stat${down > 0 ? ' stat-bad' : ''}`}>
          <span className="stat-label">{t('dashboard:stat.sites')}</span>
          <strong className="stat-value">{down}</strong>
          <span className="hint">{t('dashboard:stat.sitesHint')}</span>
        </div>
        <div className={`stat${outdated > 0 ? ' stat-warn' : ''}`}>
          <span className="stat-label">{t('dashboard:stat.updates')}</span>
          <strong className="stat-value">{outdated}</strong>
          <span className="hint">{t('dashboard:stat.updatesHint')}</span>
        </div>
        <Link to="/notifications" className="stat stat-link">
          <span className="stat-label">{t('dashboard:stat.unread')}</span>
          <strong className="stat-value">{unread.data?.unread ?? 0}</strong>
          <span className="hint">{t('dashboard:stat.unreadHint')}</span>
        </Link>
      </div>

      <h2>{t('dashboard:attention.title')}</h2>
      {attention.length === 0 ? (
        <p className="all-good">
          <CheckCircle2 size={18} aria-hidden="true" /> {t('dashboard:attention.none')}
        </p>
      ) : (
        <ul className="links attention">
          {attention.map((a) => (
            <li key={a.key}>
              <span className="attention-icon">{a.icon}</span>
              <Link to={a.to}>{a.text}</Link>
            </li>
          ))}
        </ul>
      )}

      <h2>{t('dashboard:machines')}</h2>
      {nodes.data?.length === 0 && (
        <div className="empty">
          <p>{t('dashboard:noMachines')}</p>
          <Link to="/nodes" className="primary button-link">
            <Server size={16} aria-hidden="true" /> {t('dashboard:addMachine')}
          </Link>
        </div>
      )}
      <ul className="cards node-cards">
        {list.map((n, i) => {
          const w = worker[i]?.data
          return (
            <li key={n.id}>
              <Link to={`/nodes/${n.id}`} className="node-card">
                <span className="node-icon" aria-hidden="true">
                  <NodeIcon type={n.type} />
                </span>
                <span className="node-card-body">
                  <strong>{n.name}</strong>
                  <span className="node-meta">
                    <span className={`status-dot ${w?.connected ? 'on' : w ? 'off' : ''}`} aria-hidden="true" />
                    <span className="hint">
                      {w === undefined ? '…' : w.connected ? t('dashboard:online') : t('dashboard:offline')}
                    </span>
                  </span>
                </span>
              </Link>
            </li>
          )
        })}
      </ul>

      {can('audit.read') && (
        <>
          <h2>{t('dashboard:activity')}</h2>
          {activity.data?.entries.length === 0 && <p className="empty">{t('dashboard:noActivity')}</p>}
          <ul className="activity">
            {activity.data?.entries.map((e) => (
              <li key={e.id}>
                <span className="activity-time">{new Date(e.at).toLocaleString()}</span>
                <span>
                  <strong>{e.actor_name}</strong> · {t(`audit:event.${e.event}`, { defaultValue: e.event })}
                </span>
                {e.outcome !== 'success' && <span className="badge danger-badge">{t(`audit:outcome.${e.outcome}`, { defaultValue: e.outcome })}</span>}
              </li>
            ))}
          </ul>
          <p>
            <Link to="/audit">{t('dashboard:allActivity')}</Link>
          </p>
        </>
      )}
    </>
  )
}

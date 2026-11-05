import { useQuery } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface Stats {
  cpu_percent: number
  cpu_count: number
  load: number[]
  memory_total: number
  memory_available: number
  swap_total: number
  swap_free: number
  uptime_seconds: number
  network_received: number
  network_sent: number
  temperature: number | null
  disks: { name: string; total: number; free: number }[]
}

const gib = (n: number) => `${(n / 1073741824).toFixed(1)} GiB`

// One figure as a card: its name, the value in words, and a bar that turns amber then red as the
// resource fills up. The bar is a `meter` for assistive technology too.
function Meter({ label, value, max, text }: { label: string; value: number; max: number; text: string }) {
  const share = Math.max(0, Math.min(1, max > 0 ? value / max : 0))
  const tone = share >= 0.9 ? 'danger' : share >= 0.7 ? 'warn' : 'ok'
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <strong className="stat-value">{text}</strong>
      <div
        className={`bar bar-${tone}`}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
      >
        <span style={{ width: `${Math.round(share * 100)}%` }} />
      </div>
    </div>
  )
}

function uptime(seconds: number, t: TFunction): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  return days > 0 ? t('machine:uptimeDays', { days, hours }) : t('machine:uptimeHours', { hours, minutes: Math.floor((seconds % 3600) / 60) })
}

// The machine's own figures, read by its worker. They refresh while this tab is open.
export function MachineTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['machine', 'errors'])
  const stats = useQuery({
    queryKey: ['machine', nodeId],
    queryFn: () => api.get<Stats>(`/nodes/${nodeId}/machine`),
    refetchInterval: 6000,
  })
  if (stats.isPending) return <p>{t('loading')}</p>
  if (stats.isError) {
    return (
      <Problem error={stats.error} />
    )
  }
  const s = stats.data
  const used = s.memory_total - s.memory_available
  return (
    <section aria-label={t('machine:tab')}>
      <div className="stat-grid">
      <Meter label={t('machine:cpu', { count: s.cpu_count })} value={s.cpu_percent} max={100} text={`${s.cpu_percent}%`} />
      <Meter label={t('machine:memory')} value={used} max={s.memory_total || 1} text={`${gib(used)} / ${gib(s.memory_total)}`} />
      {s.swap_total > 0 && (
        <Meter label={t('machine:swap')} value={s.swap_total - s.swap_free} max={s.swap_total} text={`${gib(s.swap_total - s.swap_free)} / ${gib(s.swap_total)}`} />
      )}
      {s.disks.map((d) => (
        <Meter key={d.name} label={t(`machine:disk.${d.name}`)} value={d.total - d.free} max={d.total} text={t('machine:free', { free: gib(d.free), total: gib(d.total) })} />
      ))}
      </div>
      <ul className="links">
        <li>{t('machine:load', { a: s.load[0], b: s.load[1], c: s.load[2] })}</li>
        <li>{t('machine:uptime')} {uptime(s.uptime_seconds, t)}</li>
        <li>{t('machine:network', { received: gib(s.network_received), sent: gib(s.network_sent) })}</li>
        {s.temperature !== null && <li>{t('machine:temperature', { value: s.temperature })}</li>}
      </ul>
    </section>
  )
}

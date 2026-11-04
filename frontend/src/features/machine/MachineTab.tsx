import { useQuery } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

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

function Meter({ label, value, max, text }: { label: string; value: number; max: number; text: string }) {
  return (
    <p>
      <label>
        {label} <meter value={value} max={max} low={max * 0.7} high={max * 0.9} optimum={0} /> {text}
      </label>
    </p>
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
      <p role="alert" className="error">
        {errorMessage(t, stats.error)}
      </p>
    )
  }
  const s = stats.data
  const used = s.memory_total - s.memory_available
  return (
    <section aria-label={t('machine:tab')}>
      <Meter label={t('machine:cpu', { count: s.cpu_count })} value={s.cpu_percent} max={100} text={`${s.cpu_percent}%`} />
      <Meter label={t('machine:memory')} value={used} max={s.memory_total || 1} text={`${gib(used)} / ${gib(s.memory_total)}`} />
      {s.swap_total > 0 && (
        <Meter label={t('machine:swap')} value={s.swap_total - s.swap_free} max={s.swap_total} text={`${gib(s.swap_total - s.swap_free)} / ${gib(s.swap_total)}`} />
      )}
      {s.disks.map((d) => (
        <Meter key={d.name} label={t(`machine:disk.${d.name}`)} value={d.total - d.free} max={d.total} text={t('machine:free', { free: gib(d.free), total: gib(d.total) })} />
      ))}
      <ul className="links">
        <li>{t('machine:load', { a: s.load[0], b: s.load[1], c: s.load[2] })}</li>
        <li>{t('machine:uptime')} {uptime(s.uptime_seconds, t)}</li>
        <li>{t('machine:network', { received: gib(s.network_received), sent: gib(s.network_sent) })}</li>
        {s.temperature !== null && <li>{t('machine:temperature', { value: s.temperature })}</li>}
      </ul>
    </section>
  )
}

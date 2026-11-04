import { useTranslation } from 'react-i18next'

// "When" of anything that runs by itself (scheduled tasks today, image checks and Git pulls
// later): ready-made choices first, the exact controls next, a cron expression for the rest.
export type Schedule =
  | { kind: 'interval'; every_minutes: number }
  | { kind: 'daily'; at: string; timezone: string; weekdays: number[] }
  | { kind: 'cron'; expr: string; timezone: string }

export type Preset = 'hourly' | 'nightly' | 'weekly' | 'daily' | 'interval' | 'cron'

export interface When {
  preset: Preset
  every: string
  at: string
  weekdays: number[]
  cron: string
}

export const EMPTY_WHEN: When = { preset: 'nightly', every: '1440', at: '03:00', weekdays: [], cron: '0 3 * * *' }
export const zone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

const PRESETS: Preset[] = ['hourly', 'nightly', 'weekly', 'daily', 'interval', 'cron']
const DAYS = [0, 1, 2, 3, 4, 5, 6]
// Examples a person can pick instead of writing one; each is also shown in plain words.
const CRON_EXAMPLES = ['0 * * * *', '30 2 * * *', '0 4 * * 1', '0 5 1 * *', '0 */6 * * *']

// Turn what the person chose into the schedule the server understands.
export function toSchedule(w: When): Schedule {
  const tz = zone()
  switch (w.preset) {
    case 'hourly':
      return { kind: 'interval', every_minutes: 60 }
    case 'nightly':
      return { kind: 'daily', at: '03:00', timezone: tz, weekdays: [] }
    case 'weekly':
      return { kind: 'daily', at: '03:00', timezone: tz, weekdays: [0] }
    case 'daily':
      return { kind: 'daily', at: w.at, timezone: tz, weekdays: w.weekdays }
    case 'interval':
      return { kind: 'interval', every_minutes: Number(w.every) }
    default:
      return { kind: 'cron', expr: w.cron.trim(), timezone: tz }
  }
}

export function WhenPicker({ value, onChange }: { value: When; onChange: (w: When) => void }) {
  const { t } = useTranslation('schedule')
  const set = (patch: Partial<When>) => onChange({ ...value, ...patch })
  return (
    <>
      <label>
        {t('kind')}
        <select value={value.preset} onChange={(e) => set({ preset: e.target.value as Preset })}>
          {PRESETS.map((p) => (
            <option key={p} value={p}>
              {t(`presets.${p}`)}
            </option>
          ))}
        </select>
      </label>
      {value.preset === 'interval' && (
        <label>
          {t('every')}
          <input type="number" min={15} max={10080} value={value.every} required onChange={(e) => set({ every: e.target.value })} />
        </label>
      )}
      {value.preset === 'daily' && (
        <>
          <label>
            {t('at', { zone: zone() })}
            <input type="time" value={value.at} required onChange={(e) => set({ at: e.target.value })} />
          </label>
          <fieldset>
            <legend>{t('weekdays')}</legend>
            {DAYS.map((d) => (
              <label key={d}>
                <input
                  type="checkbox"
                  checked={value.weekdays.includes(d)}
                  onChange={(e) => set({ weekdays: e.target.checked ? [...value.weekdays, d] : value.weekdays.filter((x) => x !== d) })}
                />
                {t(`days.${d}`)}
              </label>
            ))}
            <p className="hint">{t('everyDayHint')}</p>
          </fieldset>
        </>
      )}
      {value.preset === 'cron' && (
        <>
          <label>
            {t('cron.label', { zone: zone() })}
            <input value={value.cron} required maxLength={100} spellCheck={false} onChange={(e) => set({ cron: e.target.value })} />
          </label>
          <p className="hint">{t('cron.help')}</p>
          <ul className="links">
            {CRON_EXAMPLES.map((example) => (
              <li key={example}>
                <button type="button" onClick={() => set({ cron: example })}>
                  <code>{example}</code>
                </button>{' '}
                {t(`cron.examples.${example}`)}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}

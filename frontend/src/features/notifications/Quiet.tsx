import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Setting {
  enabled: boolean
  start: string
  end: string
  timezone: string
}

// Do-not-disturb hours: outside channels stay silent then, except for critical events. The
// inbox always keeps everything.
export function Quiet() {
  const { t } = useTranslation(['notifications', 'errors'])
  const client = useQueryClient()
  const current = useQuery({ queryKey: ['quiet'], queryFn: () => api.get<Setting>('/notifications/quiet') })
  // What the person changed so far, on top of what the server has.
  const [edits, setEdits] = useState<Partial<Setting>>({})
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const form: Setting | null = current.data
    ? { ...current.data, timezone: current.data.timezone || browserZone, ...edits }
    : null
  const setForm = (value: Setting) => setEdits(value)
  const save = useMutation({
    mutationFn: (value: Setting) => api.put<Setting>('/notifications/quiet', value),
    onSuccess: () => {
      setEdits({})
      return client.invalidateQueries({ queryKey: ['quiet'] })
    },
  })
  if (!form) return null
  function submit(event: FormEvent) {
    event.preventDefault()
    save.mutate(form!)
  }
  return (
    <form onSubmit={submit} aria-label={t('notifications:quiet.title')}>
      <h3>{t('notifications:quiet.title')}</h3>
      <p>{t('notifications:quiet.explain')}</p>
      {(current.error ?? save.error) != null && (
        <p role="alert" className="error">
          {errorMessage(t, current.error ?? save.error)}
        </p>
      )}
      <label>
        <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> {t('notifications:quiet.enabled')}
      </label>
      <label>
        {t('notifications:quiet.from')}
        <input type="time" value={form.start} required onChange={(e) => setForm({ ...form, start: e.target.value })} />
      </label>
      <label>
        {t('notifications:quiet.to')}
        <input type="time" value={form.end} required onChange={(e) => setForm({ ...form, end: e.target.value })} />
      </label>
      <p className="hint">{t('notifications:quiet.zone', { zone: form.timezone })}</p>
      <button type="submit" disabled={save.isPending}>
        {t('notifications:quiet.save')}
      </button>
      {save.isSuccess && <span role="status"> {t('notifications:quiet.saved')}</span>}
    </form>
  )
}

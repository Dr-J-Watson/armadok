import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { rememberThisBrowser } from '../../core/push'
import { Problem } from '../../components/Problem'

// The browser wants the server's public key as bytes; the server sends it as base64url.
export function keyToBytes(text: string): Uint8Array<ArrayBuffer> {
  const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4)
  const raw = atob(padded)
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

export function pushSupported(): boolean {
  return typeof window !== 'undefined' && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
}

type Failure = 'denied' | 'noWorker'

// Turn on notifications in THIS browser. Asked for on a click only, with the explanation first:
// the browser's own push service (run by its maker) carries the message, but the message is
// encrypted so that only this browser can read it.
export function BrowserPush({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation(['notifications', 'errors'])
  const [name, setName] = useState(t('notifications:push.defaultName'))
  const [failure, setFailure] = useState<Failure | null>(null)
  const enable = useMutation({
    mutationFn: async () => {
      setFailure(null)
      if ((await Notification.requestPermission()) !== 'granted') {
        setFailure('denied')
        return false
      }
      const registration = await navigator.serviceWorker.getRegistration()
      if (!registration) {
        setFailure('noWorker')
        return false
      }
      const { public_key } = await api.get<{ public_key: string }>('/notifications/push-key')
      const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyToBytes(public_key) })
      const json = subscription.toJSON()
      const made = await api.post<{ id: string }>('/notifications/channels', {
        kind: 'webpush',
        name,
        settings: { endpoint: json.endpoint, p256dh: json.keys?.p256dh, auth: json.keys?.auth },
      })
      rememberThisBrowser(made.id)
      return true
    },
    onSuccess: (done) => {
      if (done) onDone()
    },
  })
  if (!pushSupported()) return <p className="hint">{t('notifications:push.unsupported')}</p>
  return (
    <section className="notice" aria-label={t('notifications:push.title')}>
      <h3>{t('notifications:push.title')}</h3>
      <p>{t('notifications:push.explain')}</p>
      <label>
        {t('notifications:push.name')}
        <input value={name} maxLength={60} onChange={(e) => setName(e.target.value)} />
      </label>{' '}
      <button type="button" disabled={enable.isPending || name.trim() === ''} onClick={() => enable.mutate()}>
        {t('notifications:push.enable')}
      </button>
      {failure && <p role="alert" className="error">{t(`notifications:push.${failure}`)}</p>}
      {enable.isError && <Problem error={enable.error} />}
    </section>
  )
}

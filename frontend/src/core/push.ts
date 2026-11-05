// The link between this browser and its push subscription, kept as a preference (the id of the
// channel it made, nothing secret). A shared computer must not keep showing one person's
// notifications to the next, so signing out, or removing the channel, ends the subscription.

import { api } from './api'
import { readPref, writePref } from './storage'

const KEY = 'push-channel'

export function rememberThisBrowser(channelId: string): void {
  writePref(KEY, channelId)
}

export function thisBrowserChannel(): string | null {
  const id = readPref(KEY)
  return id && /^[0-9a-f]{32}$/.test(id) ? id : null
}

// Ends this browser's subscription. With `removeChannel`, also removes the channel it served
// (a failure there is ignored: the subscription is gone, so nothing more can be delivered).
export async function leaveThisBrowser(removeChannel: boolean, onlyIfChannel?: string): Promise<void> {
  const mine = thisBrowserChannel()
  if (onlyIfChannel !== undefined && mine !== onlyIfChannel) return
  try {
    if ('serviceWorker' in navigator) {
      const registration = await navigator.serviceWorker.getRegistration()
      const subscription = await registration?.pushManager.getSubscription()
      await subscription?.unsubscribe()
    }
  } catch {
    // Nothing to end, or the browser refused: carry on.
  }
  if (removeChannel && mine) await api.del(`/notifications/channels/${mine}`).catch(() => undefined)
  writePref(KEY, '')
}

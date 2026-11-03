import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Channels } from './Channels'
import { Inbox } from './Inbox'
import { Rules } from './Rules'

const TABS = ['inbox', 'channels', 'rules'] as const

export function NotificationsPage() {
  const { t } = useTranslation('notifications')
  const [tab, setTab] = useState<(typeof TABS)[number]>('inbox')
  return (
    <>
      <h1>{t('title')}</h1>
      <div role="tablist" className="tabs">
        {TABS.map((name) => (
          <button key={name} type="button" role="tab" aria-selected={tab === name} onClick={() => setTab(name)}>
            {t(`tabs.${name}`)}
          </button>
        ))}
      </div>
      <section role="tabpanel">
        {tab === 'inbox' && <Inbox />}
        {tab === 'channels' && <Channels />}
        {tab === 'rules' && <Rules />}
      </section>
    </>
  )
}

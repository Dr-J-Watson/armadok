import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Channels } from './Channels'
import { Inbox } from './Inbox'
import { Quiet } from './Quiet'
import { Rules } from './Rules'

const TABS = ['inbox', 'channels', 'rules'] as const

export function NotificationsPage() {
  const { t } = useTranslation('notifications')
  const [tab, setTab] = useState<(typeof TABS)[number]>('inbox')
  return (
    <>
      <header className="page-head">
        <div>
          <h1>{t('title')}</h1>
          <p className="hint">{t('subtitle')}</p>
        </div>
      </header>
      <div role="tablist" className="tabs">
        {TABS.map((name) => (
          <button key={name} type="button" role="tab" aria-selected={tab === name} title={t(`tabHints.${name}`)} onClick={() => setTab(name)}>
            {t(`tabs.${name}`)}
          </button>
        ))}
      </div>
      <section role="tabpanel">
        {tab === 'inbox' && <Inbox />}
        {tab === 'channels' && <Channels />}
        {tab === 'rules' && (
          <>
            <Quiet />
            <Rules />
          </>
        )}
      </section>
    </>
  )
}

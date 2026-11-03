import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Preferences } from '../../shell/Preferences'

// Frame shared by the setup and login pages.
export function AuthLayout({ title, children }: { title: string; children: ReactNode }) {
  const { t } = useTranslation()
  return (
    <main className="auth">
      <div className="auth-card">
        <h1>{t('app.name')}</h1>
        <h2>{title}</h2>
        {children}
        <Preferences />
      </div>
    </main>
  )
}

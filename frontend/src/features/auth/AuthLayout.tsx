import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Logo } from '../../components/Logo'
import { Preferences } from '../../shell/Preferences'

// Frame shared by the setup and login pages: the name and mark, a calm card, and the language
// and look underneath (they matter before anyone has signed in).
export function AuthLayout({ title, children }: { title: string; children: ReactNode }) {
  const { t } = useTranslation()
  return (
    <main className="auth">
      <div className="auth-stack">
        <div className="auth-brand">
          <Logo size={44} />
          <h1>{t('app.name')}</h1>
          <p className="hint">{t('app.tagline')}</p>
        </div>
        <div className="auth-card">
          <h2>{title}</h2>
          {children}
        </div>
        <details className="auth-prefs">
          <summary>{t('prefs.title')}</summary>
          <Preferences />
        </details>
      </div>
    </main>
  )
}

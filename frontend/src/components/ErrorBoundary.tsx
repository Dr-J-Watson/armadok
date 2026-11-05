import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { withTranslation } from 'react-i18next'
import type { WithTranslation } from 'react-i18next'

interface State {
  failed: boolean
}

// A screen that broke must not leave a blank page. This says so in plain words and offers the
// two things a person can do: try again here, or reload. The details stay in the browser console
// (nothing is sent anywhere).
class Boundary extends Component<WithTranslation & { children: ReactNode }, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Armadok screen failed', error, info.componentStack)
  }

  render() {
    const { t, children } = this.props
    if (!this.state.failed) return children
    return (
      <main className="auth">
        <div className="auth-stack">
          <div className="auth-card" role="alert">
            <h2>{t('crash.title')}</h2>
            <p>{t('crash.text')}</p>
            <div className="actions">
              <button type="button" className="primary" onClick={() => window.location.reload()}>
                {t('crash.reload')}
              </button>
              <button type="button" onClick={() => this.setState({ failed: false })}>
                {t('crash.retry')}
              </button>
            </div>
          </div>
        </div>
      </main>
    )
  }
}

export const ErrorBoundary = withTranslation()(Boundary)

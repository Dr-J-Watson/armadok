import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ConfirmProvider } from './components/Confirm'
import { AuthProvider } from './core/auth'
import './core/i18n'
import { applyAccent, applyTheme, currentAccent, currentTheme } from './core/theme'
import './styles.css'

applyTheme(currentTheme())
applyAccent(currentAccent())

// Data is refetched when the window regains focus; failed requests are not retried blindly
// (an error code from the server is the answer, not something transient).
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: true } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ConfirmProvider>
        <App />
      </ConfirmProvider>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
)

// Installable app: the service worker only keeps the built files (see public/sw.js).
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Not installable here (private window, blocked): the interface works the same.
    })
  })
}

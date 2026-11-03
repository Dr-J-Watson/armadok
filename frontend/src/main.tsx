import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { AuthProvider } from './core/auth'
import './core/i18n'
import { applyTheme, currentTheme } from './core/theme'
import './styles.css'

applyTheme(currentTheme())

// Data is refetched when the window regains focus; failed requests are not retried blindly
// (an error code from the server is the answer, not something transient).
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: true } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
)

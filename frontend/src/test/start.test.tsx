import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { AuthProvider } from '../core/auth'
import { DISMISSED, StartPage } from '../features/start/StartPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

function server(over: { status?: object; dns?: unknown[]; channels?: unknown[]; checks?: Record<string, string>; totp?: boolean; admin?: boolean } = {}) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const path = String(input).replace('/api/v1', '')
    if (path === '/setup/status') return json({ setup_required: false })
    if (path === '/auth/refresh') return json({ access_token: 't' })
    if (path === '/auth/me')
      return json({ id: 'me', username: 'admin', totp_enabled: over.totp ?? false, grants: over.admin === false ? [] : [{ right: 'users.create', scope: '*', expires_at: null }] })
    if (path === '/system/status')
      return json({ version: '1', https: false, cookie_secure: false, encryption_key_set: true, proxy_trust: 'none', ...over.status })
    if (path === '/nodes') return json([{ id: 'n1', name: 'Home', type: 'full' }])
    if (path === '/nodes/n1/readiness')
      return json({ node: 'Home', checks: over.checks ?? { docker: 'ok', projects: 'not_configured', traefik: 'ok', certificates: 'no_right' } })
    if (path === '/dns/accounts') return json(over.dns ?? [])
    if (path === '/notifications/channels') return json(over.channels ?? [])
    return json({}, 404)
  })
}

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <MemoryRouter>
          <StartPage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
  window.localStorage.clear()
})

describe('getting started', () => {
  it('is for administrators', async () => {
    server({ admin: false })
    renderPage()
    expect(await screen.findByText('This page is for administrators.')).toBeInTheDocument()
  })

  it('shows what is ready, what is missing and what to fix, with a way to each', async () => {
    server()
    renderPage()
    expect(await screen.findByText('Home')).toBeInTheDocument()
    expect(await screen.findByText(/Behind a reverse proxy/)).toBeInTheDocument()
    expect(await screen.findByText(/Projects folder: not set up on this machine/)).toBeInTheDocument()
    expect(screen.getByText(/Docker: ready/)).toBeInTheDocument()
    expect(screen.queryByText(/Certificates file/)).not.toBeInTheDocument() // no right to look: not shown
    expect(await screen.findByText(/without HTTPS/)).toBeInTheDocument()
    expect(screen.getByText(/Turn on two-factor/)).toBeInTheDocument()
    expect(screen.getByText(/to look at/)).toBeInTheDocument()
    const links = screen.getAllByRole('link', { name: 'Open' })
    expect(links.length).toBeGreaterThan(2)
  })

  it('says everything is fine when it is', async () => {
    server({
      status: { https: true, cookie_secure: true, proxy_trust: 'restricted' },
      dns: [{}],
      channels: [{}],
      totp: true,
      checks: { docker: 'ok', projects: 'ok', traefik: 'ok', certificates: 'ok' },
    })
    renderPage()
    expect(await screen.findByText('Everything looks fine.')).toBeInTheDocument()
  })

  it('can be hidden, and remembers it', async () => {
    server()
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Hide this page for now' }))
    expect(window.localStorage.getItem(`armadok.${DISMISSED}`)).toBe('1')
    expect(screen.queryByRole('button', { name: 'Hide this page for now' })).not.toBeInTheDocument()
  })
})

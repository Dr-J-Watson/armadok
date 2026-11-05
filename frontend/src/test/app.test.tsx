import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../App'
import { setAccessToken } from '../core/api'
import { AuthProvider } from '../core/auth'
import '../core/i18n'

type Handler = (method: string, body: unknown) => unknown

// A tiny fake server: route -> answer (a Response, or a value sent as JSON 200).
function fakeServer(routes: Record<string, Handler>) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    const handler = routes[`${method} ${path}`]
    if (!handler) return new Response(JSON.stringify({ error: { code: 'common.not_found' } }), { status: 404 })
    const body = init?.body ? JSON.parse(String(init.body)) : undefined
    const answer = handler(method, body)
    return answer instanceof Response ? answer : new Response(JSON.stringify(answer), { status: 200 })
  })
}

function renderApp() {
  window.history.pushState({}, '', '/')
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

const node = (type: string) => ({
  id: 'n1', name: 'Home server', type, description: '', owner: 'u1', created_at: '2026-01-01T00:00:00Z',
})

const signedIn = {
  'GET /setup/status': () => ({ setup_required: false }),
  'POST /auth/refresh': () => ({ access_token: 'tok', expires_in: 900 }),
  'GET /auth/me': () => ({ id: 'u1', username: 'admin', grants: [] }),
}

describe('application', () => {
  it('shows the first-start page when no account exists', async () => {
    fakeServer({ 'GET /setup/status': () => ({ setup_required: true }) })
    renderApp()
    expect(await screen.findByRole('heading', { name: 'First start' })).toBeInTheDocument()
    expect(screen.getByLabelText('Install code')).toBeInTheDocument()
  })

  it('shows the login page to a visitor without a session', async () => {
    fakeServer({
      'GET /setup/status': () => ({ setup_required: false }),
      'POST /auth/refresh': () => new Response('{}', { status: 401 }),
    })
    renderApp()
    expect(await screen.findByRole('heading', { name: 'Log in' })).toBeInTheDocument()
  })

  it('lists nodes and offers the tabs of the enabled features for a Docker node', async () => {
    fakeServer({
      ...signedIn,
      'GET /modules': () => ({ modules: ['users', 'nodes', 'workers', 'docker'] }),
      'GET /nodes': () => [node('docker')],
      'GET /nodes/n1': () => node('docker'),
    })
    renderApp()
    await userEvent.click(await screen.findByRole('link', { name: /Home server/ }))
    expect(await screen.findByRole('tab', { name: 'Worker' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Containers' })).toBeInTheDocument()
  })

  it('does not offer the Containers tab on a network node, nor features whose module is off', async () => {
    fakeServer({
      ...signedIn,
      'GET /modules': () => ({ modules: ['users', 'nodes', 'workers'] }),
      'GET /nodes': () => [node('network')],
      'GET /nodes/n1': () => node('network'),
    })
    renderApp()
    await userEvent.click(await screen.findByRole('link', { name: /Home server/ }))
    expect(await screen.findByRole('tab', { name: 'Worker' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Containers' })).not.toBeInTheDocument()
  })

  it('shows a translated message when the server refuses (missing right)', async () => {
    fakeServer({
      ...signedIn,
      'GET /modules': () => ({ modules: ['users', 'nodes'] }),
      'GET /nodes': () => new Response(JSON.stringify({ error: { code: 'rights.missing' } }), { status: 403 }),
    })
    renderApp()
    // The overview leaves out what it cannot read; the machines page says why.
    await userEvent.click(await screen.findByRole('link', { name: 'Machines' }))
    expect(await screen.findByText('You do not have the right to do this.')).toBeInTheDocument()
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { DashboardPage } from '../features/dashboard/DashboardPage'

vi.mock('../core/auth', () => ({
  useAuth: () => ({ state: { status: 'authenticated', user: { username: 'alice' } } }),
  useCanFn: () => () => true,
  useCan: () => true,
}))

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const node = (id: string, name: string, type = 'full', local = false) => ({ id, name, type, description: '', owner: 'u', created_at: '', local, suspended: false })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

function server(routes: Record<string, () => Response>) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const path = String(input).replace('/api/v1', '').split('?')[0]
    return routes[path]?.() ?? json({ error: { code: 'rights.missing' } }, 403)
  })
}

function renderIt() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('overview', () => {
  it('greets, counts, and lists what needs a look: a worker away, a site down, an image to update', async () => {
    server({
      '/nodes': () => json([node('n1', 'Home', 'full'), node('n2', 'Office', 'docker')]),
      '/nodes/n1/worker': () => json({ connected: true, enrolled: true }),
      '/nodes/n2/worker': () => json({ connected: false, enrolled: true }),
      '/nodes/n1/uptime': () => json({ probes: [{ name: 'blog', state: 'down', enabled: true }, { name: 'shop', state: 'up', enabled: true }] }),
      '/nodes/n1/image-updates': () => json({ images: [{ ref: 'redis:7', state: 'update' }, { ref: 'nginx:1', state: 'current' }] }),
      '/nodes/n2/image-updates': () => json({ images: [] }),
      '/notifications/unread-count': () => json({ unread: 3 }),
      '/audit': () => json({ entries: [{ id: 1, at: '2026-11-04T10:00:00+00:00', actor_name: 'bob', event: 'auth.login', outcome: 'success' }] }),
    })
    renderIt()
    expect(await screen.findByRole('heading', { name: 'Hello, alice' })).toBeInTheDocument()
    expect(await screen.findByText('Office is not connected: its worker is not answering.')).toBeInTheDocument()
    expect(await screen.findByText('blog does not respond on Home.')).toBeInTheDocument()
    expect(await screen.findByText('1 image(s) have a newer version on Home.')).toBeInTheDocument()
    expect(screen.getByText('1 connected')).toBeInTheDocument()
    expect(await screen.findByText('3')).toBeInTheDocument() // unread notifications
    expect(await screen.findByText(/bob/)).toBeInTheDocument() // recent activity
  })

  it('says everything is fine when nothing needs a look, and leaves out what it may not read', async () => {
    server({ '/nodes': () => json([node('n1', 'Home', 'full', true)]), '/nodes/n1/worker': () => json({ connected: true, enrolled: true }) })
    renderIt()
    expect(await screen.findByText('Everything looks fine.')).toBeInTheDocument()
  })
})

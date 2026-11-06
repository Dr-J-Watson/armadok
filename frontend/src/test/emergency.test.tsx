import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { EmergencyBanner } from '../features/emergency/EmergencyBanner'
import { EmergencyTab } from '../features/emergency/EmergencyTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const calls: { method: string; path: string }[] = []

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('emergency', () => {
  it('shows a banner with the time left and lets the administrator end the session', async () => {
    calls.length = 0
    const soon = new Date(Date.now() + 5 * 60 * 1000).toISOString()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input).replace('/api/v1', '')
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method !== 'GET') calls.push({ method, path })
      return path === '/emergency/active'
        ? json([{ id: 's1', node_id: 'n1', node_name: 'Olga box', expires_at: soon }])
        : json({}, 204)
    })
    wrap(<EmergencyBanner />)
    expect(await screen.findByText(/Emergency access to Olga box: 4:5\d left|Emergency access to Olga box: 5:00 left/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'End the session' }))
    expect(calls[0]).toEqual({ method: 'DELETE', path: '/admin/nodes/n1/emergency' })
  })

  it('shows nothing once the session is over', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json([{ id: 's1', node_id: 'n1', node_name: 'Olga box', expires_at: new Date(Date.now() - 1000).toISOString() }]),
    )
    wrap(<EmergencyBanner />)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('lists a notice with its actions and acknowledges it', async () => {
    calls.length = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input).replace('/api/v1', '')
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method !== 'GET') calls.push({ method, path })
      return path.endsWith('/emergency')
        ? json([{ id: 's1', admin: 'admin', reason: 'Site down, owner unreachable', started_at: new Date().toISOString(), expires_at: new Date().toISOString(), ended_at: null, open: true, acknowledged: false, actions: [{ at: new Date().toISOString(), event: 'node.update', outcome: 'success' }] }])
        : json({}, 204)
    })
    wrap(<EmergencyTab nodeId="n1" />)
    expect(await screen.findByText(/Site down, owner unreachable/)).toBeInTheDocument()
    expect(screen.getByText('1 action(s) during the session')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Acknowledge' }))
    expect(calls[0]).toEqual({ method: 'POST', path: '/nodes/n1/emergency/s1/acknowledge' })
  })

  it('tells people who are not the owner that it is the owner who sees this', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ error: { code: 'rights.missing' } }, 403))
    wrap(<EmergencyTab nodeId="n1" />)
    expect(await screen.findByText('Only the owner of this node sees the emergency accesses.')).toBeInTheDocument()
  })
})

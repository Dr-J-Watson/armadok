import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { UptimeTab } from '../features/uptime/UptimeTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const probe = (over = {}) => ({
  id: 'p1', name: 'Blog', kind: 'http', scheme: 'https', host: 'blog.example.com', port: 443, path: '/',
  enabled: true, state: 'up', interval_seconds: 60, last_checked: null, last_reason: null,
  availability: 99.5, recent: [{ at: '2026-01-01T00:00:00Z', ok: true, latency_ms: 12, reason: null }], ...over,
})

function renderTab(answer: unknown, canManage = true) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
    init?.method && init.method !== 'GET' ? json({}) : json({ min_interval: 30, max_interval: 3600, can_manage: canManage, ...(answer as object) }),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <UptimeTab nodeId="n1" />
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('availability tab', () => {
  it('shows each probe with its state, why it is down, and its availability', async () => {
    renderTab({ probes: [probe(), probe({ id: 'p2', name: 'Shop', state: 'down', last_reason: 'http_503' })] })
    expect(await screen.findByText('Blog')).toBeInTheDocument()
    expect(screen.getByText('Up')).toBeInTheDocument()
    expect(screen.getByText('Down')).toBeInTheDocument()
    expect(screen.getByText(/http_503/)).toBeInTheDocument() // an unknown reason is shown as is
    expect(screen.getAllByText(/99.5% available/).length).toBe(2)
  })

  it('hides the controls from someone who can only read', async () => {
    renderTab({ probes: [probe()] }, false)
    await screen.findByText('Blog')
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
    expect(screen.queryByRole('form', { name: 'New probe' })).toBeNull()
  })

  it('creates a probe from the form', async () => {
    const spy = renderTab({ probes: [] })
    await screen.findByText('No probe yet.')
    await userEvent.type(screen.getByLabelText('Name'), 'Site')
    await userEvent.type(screen.getByLabelText('Address'), 'https://www.example.com/')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(post).toBeDefined()
    expect(JSON.parse(String(post![1]!.body))).toMatchObject({ name: 'Site', kind: 'http', url: 'https://www.example.com/', interval_seconds: 60 })
  })

  it('pauses a probe', async () => {
    const spy = renderTab({ probes: [probe()] })
    await userEvent.click(await screen.findByRole('button', { name: 'Pause' }))
    const call = spy.mock.calls.find(([, init]) => init?.method === 'PATCH')
    expect(JSON.parse(String(call![1]!.body))).toEqual({ enabled: false })
  })
})

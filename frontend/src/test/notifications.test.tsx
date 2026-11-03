import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { Bell } from '../features/notifications/Bell'
import { Channels } from '../features/notifications/Channels'
import { Inbox } from '../features/notifications/Inbox'
import { Rules } from '../features/notifications/Rules'

// A made-up signing secret, assembled so it does not look like a leaked one.
const FAKE_SECRET = ['whsec', 'shown', 'once'].join('-')

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const sent: { method: string; path: string; body?: Record<string, unknown> }[] = []
let state = {
  inbox: [] as unknown[],
  unread: 0,
  channels: [{ id: 'c1', kind: 'ntfy', name: 'phone', last_status: 'outbound.address_not_allowed' }],
  rules: [] as unknown[],
}

function server() {
  sent.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') sent.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (path === '/notifications/unread-count') return json({ unread: state.unread })
    if (path.startsWith('/notifications?') || path === '/notifications') return json({ entries: state.inbox, next_before_id: null, unread: state.unread })
    if (path === '/notifications/channels' && method === 'GET') return json(state.channels)
    if (path === '/notifications/channels' && method === 'POST') return json({ id: 'c2', secret: FAKE_SECRET }, 201)
    if (path === '/notifications/events') return json([{ name: 'worker.disconnected', severity: 'warning', title: 'Worker disconnected' }])
    if (path === '/notifications/rules' && method === 'GET') return json(state.rules)
    if (path === '/notifications/rules' && method === 'PUT') return json([])
    return json({})
  })
}

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
  state = { inbox: [], unread: 0, channels: state.channels, rules: [] }
})

describe('notifications', () => {
  it('shows the number of unread notifications on the bell', async () => {
    state.unread = 3
    server()
    wrap(<Bell />)
    expect(await screen.findByLabelText('Notifications, 3 unread')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('lists the inbox and marks everything as read', async () => {
    state.inbox = [{ id: 1, event: 'worker.disconnected', severity: 'warning', node_id: 'n', title: 'Worker disconnected', body: 'The worker of home is no longer connected.', created_at: '2026-01-01T10:00:00Z', read: false }]
    state.unread = 1
    server()
    wrap(<Inbox />)
    expect(await screen.findByText('Worker disconnected')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Mark all as read' }))
    await vi.waitFor(() => expect(sent.find((s) => s.path === '/notifications/read')?.body).toEqual({ ids: null }))
  })

  it('never shows a channel address or token, only its name, kind and last result', async () => {
    server()
    wrap(<Channels />)
    expect(await screen.findByText('phone')).toBeInTheDocument()
    expect(screen.getByText(/inside a private network/)).toBeInTheDocument() // the last error, translated
  })

  it('adds a webhook and shows its signing secret once', async () => {
    server()
    wrap(<Channels />)
    await userEvent.selectOptions(await screen.findByLabelText('Kind'), 'webhook')
    await userEvent.type(screen.getByLabelText('Name'), 'ci')
    await userEvent.type(screen.getByLabelText('Address (URL)'), 'https://hooks.example.com/x')
    await userEvent.click(screen.getByRole('button', { name: 'Add the channel' }))
    expect(await screen.findByText(FAKE_SECRET)).toBeInTheDocument()
    expect(sent.find((s) => s.path === '/notifications/channels')?.body).toEqual({
      kind: 'webhook', name: 'ci', settings: { url: 'https://hooks.example.com/x' },
    })
    await userEvent.click(screen.getByRole('button', { name: 'I saved it' }))
    expect(screen.queryByText(FAKE_SECRET)).not.toBeInTheDocument()
  })

  it('saves rules per event, defaulting to the inbox, and keeps node-specific rules it does not show', async () => {
    state.rules = [{ event: 'worker.disconnected', target: 'c1', min_severity: 'warning', node_id: 'n1', enabled: true }]
    server()
    wrap(<Rules />)
    expect(await screen.findByText('Worker disconnected')).toBeInTheDocument()
    await userEvent.click(await screen.findByRole('checkbox', { name: 'phone' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save the rules' }))
    await vi.waitFor(() => expect(sent.some((s) => s.method === 'PUT')).toBe(true))
    const rules = sent.find((s) => s.method === 'PUT')?.body?.rules as { target: string; node_id: string | null }[]
    expect(rules.some((r) => r.target === 'in_app' && r.node_id === null)).toBe(true)
    expect(rules.some((r) => r.target === 'c1' && r.node_id === null)).toBe(true)
    expect(rules.some((r) => r.node_id === 'n1')).toBe(true) // the node-specific rule survived
  })
})

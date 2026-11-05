import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { AuditList } from '../features/audit/AuditList'

// Only the right check is needed here, not a whole session.
vi.mock('../core/auth', () => ({ useCan: () => true }))

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const entry = (id: number, event: string, outcome = 'success', details = {}) => ({
  id, at: '2026-01-01T10:00:00Z', actor_name: 'admin', event, object_type: 'node', object_id: 'n1', outcome, details,
})
const urls: string[] = []

function server(pages: Record<string, unknown>) {
  urls.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const path = String(input).replace('/api/v1', '')
    urls.push(path)
    if (path === '/auth/refresh') return json({ access_token: 't' })
    if (path === '/audit/verify') return json({ intact: false, checked: 3, first_bad_id: 7 })
    const key = path.includes('before_id=') ? 'older' : 'first'
    return json(pages[key])
  })
}

function renderList(nodeId?: string) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuditList nodeId={nodeId} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('audit list', () => {
  it('shows translated events, results and details, and loads older entries on request', async () => {
    server({
      first: { entries: [entry(9, 'node.create'), entry(8, 'project.file_write', 'failure', { code: 'worker.conflict' })], next_before_id: 8 },
      older: { entries: [entry(7, 'auth.login')], next_before_id: null },
    })
    renderList()
    expect(await screen.findByText('Machine created')).toBeInTheDocument()
    expect(screen.getByText('Compose file saved')).toBeInTheDocument()
    expect(screen.getByText('Failed', { selector: '.badge' })).toBeInTheDocument()
    expect(screen.getByText('code: worker.conflict')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Load older entries' }))
    expect(await screen.findByText('Login')).toBeInTheDocument()
    expect(urls.some((u) => u.includes('before_id=8'))).toBe(true)
    expect(screen.queryByRole('button', { name: 'Load older entries' })).not.toBeInTheDocument()
  })

  it('limits the request to one node and applies filters on the server', async () => {
    server({ first: { entries: [], next_before_id: null }, older: { entries: [], next_before_id: null } })
    renderList('n1')
    expect(await screen.findByText('No entry matches.')).toBeInTheDocument()
    expect(urls[0]).toContain('node_id=n1')
    await userEvent.selectOptions(screen.getByRole('combobox'), 'denied')
    await vi.waitFor(() => expect(urls.some((u) => u.includes('outcome=denied'))).toBe(true))
  })

  it('shows an event name it does not know as it is rather than a blank', async () => {
    server({ first: { entries: [entry(1, 'future.event')], next_before_id: null }, older: { entries: [], next_before_id: null } })
    renderList()
    expect(await screen.findByText('future.event')).toBeInTheDocument()
  })
})

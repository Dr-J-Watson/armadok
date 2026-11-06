import { vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { AccountsSection } from '../features/dns/AccountsSection'
import { DomainPage } from '../features/dns/DomainPage'
import { DomainsSection } from '../features/dns/DomainsSection'
import { NodeDnsTab } from '../features/dns/NodeDnsTab'

// A made-up application secret, assembled so it does not look like a leaked one.
const FAKE_APP_SECRET = ['appsecret', 'value', '1234'].join('')

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const sent: { method: string; path: string; body?: Record<string, unknown> }[] = []
let domain = { id: 'd1', name: 'example.com', node_id: null, access: 'owner', capabilities: ['records', 'write', 'zones'], provider: 'ovh', account_name: 'main' }
const report = [
  { host: 'blog.example.com', domain_id: 'd1', status: 'present', resolves: ['203.0.113.7'] },
  { host: 'www.example.com', domain_id: 'd1', status: 'missing', resolves: [] },
]

function server() {
  sent.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') sent.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (path === '/dns/accounts' && method === 'GET') return json([{ id: 'a1', provider: 'ovh', name: 'main' }])
    if (path === '/dns/domains') return json([domain])
    if (path === '/nodes') return json([{ id: 'n1', name: 'Home server', type: 'full', description: '', owner: 'u', created_at: '' }])
    if (path === '/dns/domains/d1/records') return json({ records: [{ id: '1', type: 'A', name: '', target: '203.0.113.7', ttl: 3600 }], capabilities: domain.capabilities })
    if (path === '/nodes/n1/dns/report') return json({ hosts: report })
    if (method !== 'GET') return json({ id: 'new' }, 201)
    return json({ error: { code: 'common.not_found' } }, 404)
  })
}

const providers = [
  { name: 'local', capabilities: [], fields: [] },
  { name: 'duckdns', capabilities: ['records', 'publish_ip'], fields: ['token'] },
  { name: 'ovh', capabilities: ['records', 'write', 'zones'], fields: ['application_key', 'application_secret', 'consumer_key', 'endpoint'] },
]

function wrap(node: React.ReactNode, path = '/') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
  domain = { id: 'd1', name: 'example.com', node_id: null, access: 'owner', capabilities: ['records', 'write', 'zones'], provider: 'ovh', account_name: 'main' }
})

describe('dns', () => {
  it('asks for the fields the provider declares, hides keys, and does not keep them after sending', async () => {
    server()
    wrap(<AccountsSection providers={providers} />)
    await userEvent.selectOptions(await screen.findByLabelText('Provider'), 'ovh')
    const secret = screen.getByLabelText('Application secret')
    expect(secret).toHaveAttribute('type', 'password')
    expect(screen.getByLabelText('Consumer key')).toHaveAttribute('type', 'password')
    await userEvent.type(screen.getByLabelText('A name for yourself'), 'main')
    for (const [label, value] of [['Application key', 'appkey1234'], ['Application secret', FAKE_APP_SECRET], ['Consumer key', 'consumerkeyvalue12']])
      await userEvent.type(screen.getByLabelText(label), value)
    await userEvent.click(screen.getByRole('button', { name: 'Add the account' }))
    await vi.waitFor(() => expect(sent.some((s) => s.path === '/dns/accounts')).toBe(true))
    expect(sent[0].body).toMatchObject({ provider: 'ovh', name: 'main', credentials: { application_key: 'appkey1234', application_secret: FAKE_APP_SECRET } })
    await vi.waitFor(() => expect(screen.getByLabelText('Application secret')).toHaveValue(''))
    // The list of accounts shows names only.
    expect(screen.queryByText(new RegExp(FAKE_APP_SECRET))).not.toBeInTheDocument()
  })

  it('lets the owner add and delete records on a provider that can write', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true) // the removal now asks first
    server()
    wrap(<Routes><Route path="/dns/:domainId" element={<DomainPage />} /></Routes>, '/dns/d1')
    expect(await screen.findByText('203.0.113.7')).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Name (empty for the domain itself)'), 'blog')
    await userEvent.type(screen.getByLabelText('Value'), '203.0.113.7')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await vi.waitFor(() => expect(sent.find((s) => s.method === 'POST')?.body).toMatchObject({ type: 'A', name: 'blog', target: '203.0.113.7', ttl: 3600 }))
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await vi.waitFor(() => expect(sent.some((s) => s.method === 'DELETE')).toBe(true))
  })

  it('offers no editing to a read-only member, and explains a local DNS', async () => {
    domain = { ...domain, access: 'read' }
    server()
    const { unmount } = wrap(<Routes><Route path="/dns/:domainId" element={<DomainPage />} /></Routes>, '/dns/d1')
    expect(await screen.findByText('203.0.113.7')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    unmount()
    domain = { ...domain, access: 'owner', capabilities: [], provider: 'local' }
    server()
    wrap(<Routes><Route path="/dns/:domainId" element={<DomainPage />} /></Routes>, '/dns/d1')
    expect(await screen.findByText(/uses a local DNS/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add' })).not.toBeInTheDocument()
  })

  it('shows which hosts the DNS lacks and creates the missing record in one click', async () => {
    server()
    wrap(<NodeDnsTab nodeId="n1" />)
    expect(await screen.findByText('blog.example.com')).toBeInTheDocument()
    expect(screen.getByText('203.0.113.7')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Create the record' })).toHaveLength(1) // only for the missing one
    await userEvent.click(screen.getByRole('button', { name: 'Create the record' }))
    await vi.waitFor(() => expect(sent.find((s) => s.path === '/nodes/n1/dns/fix')?.body).toEqual({ host: 'www.example.com' }))
  })

  it('warns that members use the owner\'s account and sends the limits chosen on attach', async () => {
    domain = { ...domain, member_types: ['A', 'AAAA', 'CNAME'], member_apex: false, member_names: [] } as typeof domain
    server()
    wrap(<DomainsSection />)
    await userEvent.selectOptions(await screen.findByLabelText(/Attached to/), 'n1')
    expect(await screen.findByText(/using YOUR provider account/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'MX' })).not.toBeChecked() // narrow by default
    expect(screen.getByRole('checkbox', { name: /^A$/ })).toBeChecked()
    await userEvent.click(screen.getByRole('checkbox', { name: 'TXT' }))
    await userEvent.type(screen.getByLabelText(/Only these names/), 'dev, staging')
    await userEvent.click(screen.getByRole('button', { name: 'Attach' }))
    await vi.waitFor(() => expect(sent.find((x) => x.method === 'PATCH')?.body).toEqual({
      id: 'd1', node_id: 'n1', member_types: ['A', 'AAAA', 'CNAME', 'TXT'], member_apex: false, member_names: ['dev', 'staging'],
    }))
  })
})

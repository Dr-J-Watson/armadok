import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ServiceAccountsPage } from '../features/service_accounts/ServiceAccountsPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const SHOWN = ['svc', 'key', 'shown'].join('-')
const calls: { method: string; path: string; body: unknown }[] = []
let accounts: unknown[] = []

function server() {
  calls.length = 0
  accounts = [{ id: 'a'.repeat(32), username: 'svc-ci', is_active: true, follow_owner: true, description: 'builds' }]
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : null })
    if (path === '/service-accounts' && method === 'GET') return json(accounts)
    if (path.endsWith('/keys') && method === 'GET') return json([])
    if (path.endsWith('/keys') && method === 'POST') return json({ id: 'k1', key: SHOWN }, 201)
    if (path === '/rights')
      return json([
        { id: 'nodes.read', depends_on: [], sensitive: false, node_owner: true },
        { id: 'projects.deploy', depends_on: [], sensitive: true, node_owner: true },
      ])
    return json({}, 201)
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('service accounts', () => {
  it('creates an account that follows the person, then a key with chosen non-sensitive rights shown once', async () => {
    server()
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <ServiceAccountsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await userEvent.type(await screen.findByLabelText(/^Name/), 'deploy')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(calls[0]).toMatchObject({ path: '/service-accounts', body: { name: 'deploy', follow_owner: true } })

    await userEvent.click(await screen.findByRole('button', { name: 'Keys' }))
    // A sensitive right is not even offered: no key can carry it.
    expect(await screen.findByText('nodes.read')).toBeInTheDocument()
    expect(screen.queryByText('projects.deploy')).not.toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Key name'), 'ci key')
    await userEvent.click(screen.getByLabelText('nodes.read'))
    await userEvent.click(screen.getByRole('button', { name: 'Create the key' }))
    expect(await screen.findByDisplayValue(SHOWN)).toBeInTheDocument()
    expect(calls.at(-1)).toMatchObject({ body: { name: 'ci key', rights: ['nodes.read'] } })
    await userEvent.click(screen.getByRole('button', { name: 'I have copied it' }))
    expect(screen.queryByDisplayValue(SHOWN)).not.toBeInTheDocument()
  })
})

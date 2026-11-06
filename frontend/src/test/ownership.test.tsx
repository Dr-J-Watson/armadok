import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { OwnershipPage } from '../features/ownership/OwnershipPage'
import { OwnershipSection } from '../features/ownership/OwnershipSection'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const sent: { method: string; path: string; body?: Record<string, unknown> }[] = []

let owners = { types: ['project'], node_owner: true, can_take: true, mine: [], others: [] as unknown[] }
let offers = { sent: [] as unknown[], received: [] as unknown[] }

function server() {
  sent.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') {
      sent.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      return json({})
    }
    if (path.startsWith('/nodes/n1/ownership/people')) return json([{ id: 'u2', username: 'bob' }])
    if (path.startsWith('/nodes/n1/ownership/cascade')) return json({ items: [{ type: 'project', id: 'blog' }] })
    if (path === '/nodes/n1/ownership') return json(owners)
    return json(offers)
  })
}

const wrap = (node: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
  owners = { types: ['project'], node_owner: true, can_take: true, mine: [], others: [] }
  offers = { sent: [], received: [] }
})

describe('ownership section', () => {
  it('is hidden for a kind that cannot have an owner', async () => {
    server()
    wrap(<OwnershipSection nodeId="n1" objectType="router" objectId="r1" />)
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled())
    expect(screen.queryByText('Owner')).toBeNull()
  })

  it('finds the receiver by exact name and sends a gift with the notes choice and the code', async () => {
    server()
    wrap(<OwnershipSection nodeId="n1" objectType="project" objectId="blog" />)
    await userEvent.type(await screen.findByLabelText('User name of the receiver (exact)'), 'bob')
    await userEvent.click(screen.getByRole('button', { name: 'Find' }))
    expect(await screen.findByText(/Give to bob/)).toBeInTheDocument()
    await userEvent.click(screen.getByLabelText('Hand over my private notes on it too'))
    await userEvent.type(screen.getByLabelText('Second-factor code (if your account has one)'), '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Send the gift' }))
    await vi.waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0].path).toBe('/nodes/n1/ownership/offers')
    expect(sent[0].body).toEqual({
      to_user: 'u2', with_notes: true, code: '123456', object_type: 'project', object_id: 'blog',
    })
  })

  it('offers to take back an object owned by someone else, only to the node owner', async () => {
    owners = {
      types: ['project'], node_owner: true, can_take: true, mine: [],
      others: [{ type: 'project', id: 'blog', owner: 'u2', owner_name: 'bob' }],
    }
    server()
    wrap(<OwnershipSection nodeId="n1" objectType="project" objectId="blog" />)
    expect(await screen.findByText('Owned by bob.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Take back now' }))
    await vi.waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0].path).toBe('/nodes/n1/ownership/take')
    expect(sent[0].body).toMatchObject({ object_type: 'project', object_id: 'blog' })
  })

  it('previews a cascade before it is sent', async () => {
    server()
    wrap(<OwnershipSection nodeId="n1" objectType="project" objectId="blog" />)
    await userEvent.click(await screen.findByLabelText(/Apply to everything of this kind/))
    // One preview for giving, one for taking back (the caller owns the node).
    expect(await screen.findAllByText('1 object(s) will change hands.')).toHaveLength(2)
  })
})

describe('ownership page', () => {
  it('lets the receiver accept or refuse, and the giver withdraw', async () => {
    offers = {
      received: [{ id: 'a', node_id: 'n1', node_name: 'Box', from_name: 'olga', to_name: 'me', with_notes: true, items: [{ type: 'project', id: 'blog' }] }],
      sent: [{ id: 'b', node_id: 'n1', node_name: 'Box', from_name: 'me', to_name: 'cat', with_notes: false, items: [{ type: 'project', id: 'shop' }] }],
    }
    server()
    wrap(<OwnershipPage />)
    expect(await screen.findByText('The giver also hands over their notes on these objects.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Accept' }))
    await userEvent.click(screen.getByRole('button', { name: 'Withdraw' }))
    await vi.waitFor(() => expect(sent).toHaveLength(2))
    expect(sent.map((s) => `${s.method} ${s.path}`)).toEqual([
      'POST /ownership/offers/a/accept',
      'DELETE /ownership/offers/b',
    ])
  })
})

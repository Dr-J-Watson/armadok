import { vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { MembersTab } from '../features/members/MembersTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const calls: { method: string; path: string; body: unknown }[] = []

function server(allowed = true) {
  calls.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : null })
    if (path === '/nodes/n1/members' && method === 'GET')
      return allowed
        ? json({ owner: { id: 'o', name: 'olga' }, members: [{ assignment_id: 'a1', subject_type: 'user', subject_name: 'pat', role_id: 'builtin-viewer', role_name: 'viewer', role_builtin: true, expires_at: null }] })
        : json({ error: { code: 'rights.missing' } }, 403)
    if (path === '/nodes/n1/members/roles')
      return json([{ id: 'builtin-viewer', name: 'viewer', builtin: true }, { id: 'builtin-operator', name: 'operator', builtin: true }])
    if (path === '/nodes/n1') return json({ name: 'Home' })
    if (path.startsWith('/directory/users')) return json([{ id: 'u9', username: 'patricia' }])
    return json({ id: 'x' }, 201)
  })
}

function renderTab() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <MembersTab nodeId="n1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('members', () => {
  it('lists the owner and members, invites by name, changes a role and removes', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true) // the removal now asks first
    server()
    renderTab()
    expect(await screen.findByText('olga')).toBeInTheDocument()
    expect(screen.getByText('pat')).toBeInTheDocument()
    await userEvent.type(screen.getAllByLabelText('Person')[0], 'pat')
    await screen.findByDisplayValue('pat')
    await userEvent.selectOptions(screen.getAllByLabelText('Role')[0], 'builtin-operator')
    await userEvent.clear(screen.getAllByLabelText('Person')[0])
    await userEvent.type(screen.getAllByLabelText('Person')[0], 'patricia')
    await userEvent.click(screen.getByRole('button', { name: 'Invite' }))
    expect(calls.at(-1)).toMatchObject({ path: '/nodes/n1/members', body: { user_id: 'u9', role_id: 'builtin-operator' } })
    await userEvent.selectOptions(screen.getByLabelText('Role of pat'), 'builtin-operator')
    expect(calls.at(-1)).toMatchObject({ method: 'PATCH', path: '/nodes/n1/members/a1', body: { role_id: 'builtin-operator' } })
    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(calls.at(-1)).toMatchObject({ method: 'DELETE', path: '/nodes/n1/members/a1' })
  })

  it('only hands the node over after the name is typed', async () => {
    server()
    renderTab()
    await screen.findByText('olga')
    const pickers = screen.getAllByLabelText('Person')
    await userEvent.type(pickers[1], 'patricia')
    const go = screen.getByRole('button', { name: 'Hand it over' })
    expect(go).toBeDisabled()
    await userEvent.type(screen.getByLabelText(/Type the name of the node/), 'Home')
    await userEvent.type(screen.getByLabelText('Second-factor code'), '123456')
    expect(go).toBeEnabled()
    await userEvent.click(go)
    expect(calls.at(-1)).toMatchObject({ path: '/nodes/n1/transfer', body: { user_id: 'u9', confirm_name: 'Home', code: '123456' } })
  })

  it('says so when you may not manage the members', async () => {
    server(false)
    renderTab()
    expect(await screen.findByText('You cannot manage the members of this node.')).toBeInTheDocument()
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { AuthProvider } from '../core/auth'
import { AccessPage } from '../features/roles/AccessPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const calls: { method: string; path: string; body: unknown }[] = []

function server() {
  calls.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : null })
    if (path === '/setup/status') return json({ setup_required: false })
    if (path === '/auth/refresh') return json({ access_token: 't' })
    if (path === '/auth/me')
      return json({ id: 'me', username: 'admin', grants: [{ right: 'grants.manage', scope: '*', expires_at: null }] })
    if (path === '/roles')
      return json([
        { id: 'builtin-viewer', name: 'viewer', builtin: true, rights: ['nodes.read', 'docker.read'] },
        { id: 'r1', name: 'Night shift', builtin: false, rights: ['nodes.read'] },
      ])
    if (path === '/rights') return json([{ id: 'nodes.read', depends_on: [], sensitive: false, node_owner: true }])
    if (path === '/assignments')
      return json([{ id: 'a1', subject_type: 'user', subject_id: 'u1', subject_name: 'bob', role_id: 'builtin-viewer', role_name: 'viewer', role_builtin: 1, scope: '*', expires_at: null }])
    if (path === '/users') return json([{ id: 'u1', username: 'bob' }])
    if (path === '/groups' || path === '/nodes') return json([])
    return json({ id: 'new' }, 201)
  })
}

function renderPage(tab: string) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <MemoryRouter initialEntries={[`/access?tab=${tab}`]}>
          <AccessPage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('access page', () => {
  it('shows built-in roles with their translated name, and only custom ones can be edited', async () => {
    server()
    renderPage('roles')
    expect(await screen.findByText('Viewer')).toBeInTheDocument()
    expect(screen.getByText('Night shift')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Copy' })).toHaveLength(2)
    await userEvent.click(screen.getAllByRole('button', { name: 'Copy' })[0])
    const form = screen.getByLabelText('Name of the copy').closest('form')!
    await userEvent.click(within(form).getByRole('button', { name: 'Copy' }))
    expect(calls.at(-1)).toMatchObject({ path: '/roles/builtin-viewer/duplicate', body: { name: 'Viewer (2)' } })
  })

  it('lists assignments and takes one away', async () => {
    server()
    renderPage('assignments')
    expect(await screen.findByRole('cell', { name: /bob/ })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Take away' }))
    expect(calls.at(-1)).toMatchObject({ method: 'DELETE', path: '/assignments/a1' })
  })
})

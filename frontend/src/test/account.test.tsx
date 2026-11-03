import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { AuthProvider } from '../core/auth'
import { AccountPage } from '../features/account/AccountPage'
import { withDependencies, withoutDependents } from '../features/account/rights'
import type { RightInfo } from '../features/account/rights'
import { UsersPage } from '../features/users/UsersPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

const CATALOGUE: RightInfo[] = [
  { id: 'nodes.read', depends_on: [], sensitive: false, node_owner: true },
  { id: 'nodes.update', depends_on: ['nodes.read'], sensitive: false, node_owner: true },
  { id: 'docker.read', depends_on: ['nodes.read'], sensitive: false, node_owner: true },
  { id: 'grants.manage', depends_on: [], sensitive: true, node_owner: false },
]

// A made-up authenticator key, assembled so it does not look like a leaked secret.
const FAKE_KEY = ['JBSWY3DP', 'EHPK3PXP'].join('')

const grant = (right: string) => ({ right, scope: '*', expires_at: null })
let me = {
  id: 'u1', username: 'admin', totp_enabled: false, backup_codes_remaining: 0,
  grants: ['nodes.read', 'nodes.update', 'grants.manage', 'apikeys.own', 'users.read', 'users.create'].map(grant),
}
const sent: { path: string; body: unknown }[] = []

function server() {
  sent.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') sent.push({ path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (path === '/setup/status') return json({ setup_required: false })
    if (path === '/auth/refresh') return json({ access_token: 't' })
    if (path === '/auth/me') return json(me)
    if (path === '/modules') return json({ modules: ['users', 'apikeys', 'nodes'] })
    if (path === '/rights') return json(CATALOGUE)
    if (path === '/apikeys' && method === 'GET') return json([])
    if (path === '/apikeys' && method === 'POST') return json({ id: 'k', key: 'arm_abc_secret', name: 'ci' }, 201)
    if (path === '/auth/2fa/enroll') return json({ secret: FAKE_KEY, otpauth_uri: `otpauth://totp/Armadok:admin?secret=${FAKE_KEY}` })
    if (path === '/auth/2fa/confirm') return json({ backup_codes: ['aaaaaaaaaa-bbbbbbbbbb'] })
    if (path === '/users') return json([{ id: 'u1', username: 'admin', is_active: true, created_at: '' }])
    return json({ error: { code: 'common.not_found' } }, 404)
  })
}

function renderPage(page: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <MemoryRouter>{page}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('rights helpers', () => {
  it('choosing a right also chooses what it needs, dropping one drops what depends on it', () => {
    const picked = withDependencies(new Set(), CATALOGUE, 'nodes.update')
    expect([...picked].sort()).toEqual(['nodes.read', 'nodes.update'])
    const after = withoutDependents(picked, CATALOGUE, 'nodes.read')
    expect(after.size).toBe(0)
  })
})

describe('account page', () => {
  it('walks through turning on two-factor: password, QR, code, backup codes shown once', async () => {
    server()
    renderPage(<AccountPage />)
    await userEvent.type(await screen.findByLabelText('Current password'), 'correct horse battery')
    await userEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByRole('img', { name: /QR code/ })).toBeInTheDocument()
    expect(screen.getByText(FAKE_KEY)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Code from the app'), '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Confirm and turn on' }))
    expect(await screen.findByText('aaaaaaaaaa-bbbbbbbbbb')).toBeInTheDocument()
    const twofa = sent.filter((s) => s.path.startsWith('/auth/2fa'))
    expect(twofa.map((s) => s.path)).toEqual(['/auth/2fa/enroll', '/auth/2fa/confirm'])
    expect(twofa[0].body).toEqual({ password: 'correct horse battery' })
  })

  it('offers only non-sensitive rights the account holds, with their dependencies, and shows a new key once', async () => {
    server()
    renderPage(<AccountPage />)
    expect(await screen.findByRole('checkbox', { name: 'nodes.update' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'grants.manage' })).not.toBeInTheDocument() // sensitive
    expect(screen.queryByRole('checkbox', { name: 'docker.read' })).not.toBeInTheDocument() // not held
    await userEvent.type(screen.getByLabelText('Name (what it is used for)'), 'ci')
    await userEvent.click(screen.getByRole('checkbox', { name: 'nodes.update' }))
    expect(screen.getByRole('checkbox', { name: 'nodes.read' })).toBeChecked() // pulled in
    await userEvent.click(screen.getByRole('button', { name: 'Create the key' }))
    expect(await screen.findByText('arm_abc_secret')).toBeInTheDocument()
    const body = sent.find((s) => s.path === '/apikeys')!.body as { rights: string[] }
    expect(body.rights.sort()).toEqual(['nodes.read', 'nodes.update'])
  })
})

describe('users page', () => {
  const baseGrants = me.grants

  afterEach(() => {
    me = { ...me, grants: baseGrants }
  })

  it('lists accounts and shows the creation form to someone who may create', async () => {
    server()
    renderPage(<UsersPage />)
    expect(await screen.findByRole('link', { name: /admin/ })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Create the account' })).toBeInTheDocument()
  })

  it('hides the creation form from someone who may not create accounts', async () => {
    me = { ...me, grants: baseGrants.filter((g) => g.right !== 'users.create') }
    server()
    renderPage(<UsersPage />)
    expect(await screen.findByRole('link', { name: /admin/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create the account' })).not.toBeInTheDocument()
  })
})

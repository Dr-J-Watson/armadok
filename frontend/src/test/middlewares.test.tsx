import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { MemoryRouter } from 'react-router-dom'
import { MiddlewaresPage } from '../features/traefik/MiddlewaresPage'

// Assembled, so that no secret-looking literal sits in the source.
const PW = ['a', 'long', 'password'].join('-')
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const item = (over = {}) => ({
  name: 'private', path: 'middlewares/private.yml', sha256: 'a'.repeat(64), editable: true, used_by: [],
  spec: { intent: 'basic_auth', name: 'private', remove_header: true, users: [{ username: 'alice', has_password: true }] }, ...over,
})

// What the files hold (every middleware, written by hand or not) and what the form manages.
const fileOf = (m: { name: string; spec: { intent: string } }) => ({
  name: m.name, id: `${m.name}@file`, type: m.spec.intent === 'compress' ? 'compress' : 'basicAuth', path: `middlewares/${m.name}.yml`, refs: [],
})

function renderIt(items: unknown[], routers: unknown[] = [], extraFiles: unknown[] = []) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (init?.method && init.method !== 'GET') return json({ name: 'x', reference: 'x@file', content: 'http: {}' }, 201)
    if (String(url).endsWith('/traefik/items/overview'))
      return json({ routers, services: [], middlewares: [...(items as Parameters<typeof fileOf>[0][]).map(fileOf), ...extraFiles], errors: [] })
    return json({ middlewares: items })
  })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <MiddlewaresPage nodeId="n1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return spy
}

const startNew = async (what: string) => {
  await userEvent.click(await screen.findByRole('button', { name: 'New middleware' }))
  await userEvent.click(await screen.findByRole('button', { name: new RegExp(what) }))
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('middlewares', () => {
  it('creates "only my network" with the private ranges as a starting point, in a drawer', async () => {
    const spy = renderIt([])
    await startNew('Only my network')
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await userEvent.clear(screen.getByLabelText('Name'))
    await userEvent.type(screen.getByLabelText('Name'), 'lan')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const post = spy.mock.calls.find(([url, init]) => init?.method === 'POST' && String(url).endsWith('/traefik/items'))
    const sent = JSON.parse(String(post![1]!.body))
    expect(sent).toMatchObject({ kind: 'middlewares', name: 'lan' })
    expect(sent.value.ipAllowList.sourceRange).toContain('192.168.0.0/16')
  })

  it('turns a typed password into a hash on the server and keeps only the hash in the form', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
      if (String(url).endsWith('/htpasswd')) return json({ entry: 'alice:HASH' })
      if (init?.method === 'POST') return json({ path: 'middlewares/auth.yml', name: 'auth' }, 201)
      if (String(url).endsWith('/traefik/items/overview')) return json({ routers: [], services: [], middlewares: [], errors: [] })
      return json({ middlewares: [] })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <MiddlewaresPage nodeId="n1" />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await startNew('Ask for a password')
    await userEvent.type(await screen.findByLabelText('User name'), 'alice')
    await userEvent.type(screen.getByLabelText(/^Password/), PW)
    await userEvent.click(screen.getByRole('button', { name: 'Generate a user' }))
    await vi.waitFor(() => expect(screen.getByLabelText('users')).toHaveValue('alice:HASH'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const saved = spy.mock.calls.filter(([, init]) => init?.method === 'POST').map(([url, init]) => ({ url: String(url), body: JSON.parse(String(init!.body)) }))
    expect(saved.find((c) => c.url.endsWith('/htpasswd'))!.body).toEqual({ username: 'alice', password: PW })
    const created = saved.find((c) => c.url.endsWith('/traefik/items'))!.body
    expect(created.value.basicAuth.users).toEqual(['alice:HASH'])
    expect(JSON.stringify(created)).not.toContain(PW) // the password itself is never sent with the item
  })

  it('editing keeps the stored password unless a new one is typed', async () => {
    const spy = renderIt([item()])
    await userEvent.click(await screen.findByRole('button', { name: 'Edit private' }))
    const password = screen.getByLabelText(/Password/)
    expect(password).toHaveValue('')
    expect(password).toHaveAttribute('placeholder', 'unchanged')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const put = spy.mock.calls.find(([, init]) => init?.method === 'PUT')
    expect(JSON.parse(String(put![1]!.body)).spec.users).toEqual([{ username: 'alice' }])
    expect(JSON.parse(String(put![1]!.body)).expected_sha256).toBe('a'.repeat(64))
  })

  it('does not let a middleware that sites use be deleted, and edits a hand-written one in place', async () => {
    renderIt(
      [item()],
      [{ name: 'blog', middlewares: ['private@file'] }],
      [{ name: 'old', id: 'old@file', type: 'chain', path: 'middlewares/old.yml', refs: [] }],
    )
    expect(await screen.findByText('blog')).toBeInTheDocument() // who uses it
    expect(screen.getByRole('button', { name: 'Delete private' })).toBeDisabled()
    // The hand-written one is edited in place (its item only) and its whole file stays one click away.
    expect(screen.getByRole('link', { name: 'Open the file old' })).toHaveAttribute('href', expect.stringContaining('middlewares%2Fold.yml'))
    expect(screen.getByRole('button', { name: 'Edit old' })).toBeInTheDocument()
  })

  it('sorts middlewares by what they do', async () => {
    renderIt([item()], [], [{ name: 'old', id: 'old@file', type: 'chain', path: 'middlewares/old.yml', refs: [] }])
    expect(await screen.findByRole('region', { name: 'Security and access' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Groups' })).toBeInTheDocument()
  })

  it('previews the generated file of a managed middleware without saving', async () => {
    const spy = renderIt([item({ name: 'lan', spec: { intent: 'compress', name: 'lan', min_bytes: 1 } })])
    await userEvent.click(await screen.findByRole('button', { name: 'Edit lan' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Preview the file' }))
    expect(await screen.findByText('http: {}')).toBeInTheDocument()
    expect(String(spy.mock.calls.find(([, init]) => init?.method === 'POST')![0])).toContain('/preview')
  })
})

describe('renaming a middleware', () => {
  it('shows the plan first, warns about hand-written files, and only then renames', async () => {
    const sent: { path: string; body: Record<string, unknown> }[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        sent.push({ path: String(input), body })
        return json({ sites: ['blog'], manual: ['legacy.yml'], name: 'office', reference: 'office@file' })
      }
      const lan = item({ name: 'lan', spec: { intent: 'compress', name: 'lan' } })
      return String(input).endsWith('/traefik/items/overview')
        ? json({ routers: [], services: [], middlewares: [fileOf(lan)], errors: [] })
        : json({ middlewares: [lan] })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <MiddlewaresPage nodeId="n1" />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Rename lan' }))
    const field = screen.getByLabelText('New name')
    await userEvent.clear(field)
    await userEvent.type(field, 'office')
    await userEvent.click(screen.getByRole('button', { name: 'Show what will change' }))
    expect(await screen.findByText(/1 site\(s\) built with the form will follow the new name: blog/)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('legacy.yml')
    expect(sent).toHaveLength(1)
    expect(sent[0].body.dry_run).toBe(true) // nothing was renamed yet
    await userEvent.click(screen.getByRole('button', { name: 'Rename now' }))
    await vi.waitFor(() => expect(sent).toHaveLength(2))
    expect(sent[1].body).toEqual({ new_name: 'office', expected_sha256: 'a'.repeat(64) })
  })
})

describe('switching a middleware on and off', () => {
  it('asks the server to switch it off, then offers to switch it back on', async () => {
    const sent: { path: string; body: Record<string, unknown> }[] = []
    let off = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (init?.method === 'POST') {
        sent.push({ path: String(input), body: JSON.parse(String(init.body)) })
        off = !off
        return json({ changed: ['middlewares/old.yml'] })
      }
      if (String(input).endsWith('/traefik/items/overview'))
        return json({ routers: [], services: [], errors: [], middlewares: [{ name: 'old', id: 'old@file', type: 'chain', path: 'middlewares/old.yml', refs: [], disabled: off }] })
      return json({ middlewares: [] })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <MiddlewaresPage nodeId="n1" />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await userEvent.click(await screen.findByRole('switch', { name: 'Switch off old' }))
    expect(sent[0]).toMatchObject({ path: '/api/v1/nodes/n1/traefik/items/toggle', body: { path: 'middlewares/old.yml', kind: 'middlewares', name: 'old', enabled: false } })
    await userEvent.click(await screen.findByRole('switch', { name: 'Switch on old' }))
    expect(sent[1].body.enabled).toBe(true)
  })
})


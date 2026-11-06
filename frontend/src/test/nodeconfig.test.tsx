import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { AuthProvider } from '../core/auth'
import { NodeSettings } from '../features/nodeconfig/NodeSettings'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

const settings = (extra: object = {}) => ({
  places: {
    projects_dir: { kind: 'dir', root: '/stacks', value: '', valid: true },
    backup_dir: { kind: 'dir', root: null, value: '', valid: true },
    acme_file: { kind: 'file', root: '/acme/acme.json', value: '', valid: true },
  },
  traefik_entrypoints: ['websecure'],
  traefik_resolver: 'lehttp',
  can_write: true,
  error: null,
  ...extra,
})

function server(initial: object, check: (places: unknown) => object = () => ({ errors: {} })) {
  const calls: { method: string; path: string; body: unknown }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    const body = init?.body ? JSON.parse(String(init.body)) : null
    if (method !== 'GET') calls.push({ method, path, body })
    if (path.endsWith('/settings/check')) return json(check(body.places))
    if (path.endsWith('/settings')) return method === 'PUT' ? json({ changed: [] }) : json(initial)
    return json({ error: { code: 'rights.missing' } }, 403)
  })
  return calls
}

const wrap = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthProvider>
        <NodeSettings nodeId="n1" />
      </AuthProvider>
    </QueryClientProvider>,
  )

describe('node configuration', () => {
  it('shows each root read only and lets a sub-folder be picked, tested first then saved', async () => {
    const calls = server(settings())
    wrap()
    expect(await screen.findByText('/stacks/')).toBeInTheDocument()
    expect(screen.getByText(/Not set by the worker's environment/)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText(/Projects folder/), 'prod')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await vi.waitFor(() => expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual(['POST /nodes/n1/settings/check', 'PUT /nodes/n1/settings']))
    expect(calls[0].body).toEqual({ places: { projects_dir: 'prod' } })
    expect(calls[1].body).toMatchObject({ places: { projects_dir: 'prod' } })
  })

  it('keeps nothing and says why when the worker refuses a folder', async () => {
    const calls = server(settings(), () => ({ errors: { projects_dir: 'outside' } }))
    wrap()
    await userEvent.type(await screen.findByLabelText(/Projects folder/), '../etc')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('This leads outside the root.')).toBeInTheDocument()
    expect(calls.map((c) => c.method)).toEqual(['POST'])
  })

  it('saves the Traefik defaults as names, without asking the worker', async () => {
    const calls = server(settings())
    wrap()
    const field = await screen.findByLabelText(/Entry points for new sites/)
    await userEvent.clear(field)
    await userEvent.type(field, 'web, websecure')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await vi.waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]).toMatchObject({ method: 'PUT', body: { places: {}, traefik_entrypoints: ['web', 'websecure'] } })
  })

  it('is read only for someone who may not write', async () => {
    server(settings({ can_write: false }))
    wrap()
    expect(await screen.findByText(/You can read these settings but not change them/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(screen.getByLabelText(/Projects folder/)).toBeDisabled()
  })
})

describe('folder picker', () => {
  it('walks down the folders, refuses a project, and fills the field with the chosen path', async () => {
    const asked: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input).replace('/api/v1', '')
      if (path.includes('/settings/browse')) {
        asked.push(decodeURIComponent(path.split('path=')[1] ?? ''))
        const deeper = path.includes('path=prod')
        return json({ kind: 'dir', truncated: false, entries: deeper ? [{ name: 'web', project: true }, { name: 'more', project: false }] : [{ name: 'prod', project: false }] })
      }
      return json(settings())
    })
    wrap()
    await userEvent.click((await screen.findAllByRole('button', { name: 'Browse…' }))[0])
    await userEvent.click(await screen.findByRole('button', { name: /prod/ }))
    expect(await screen.findByRole('button', { name: /web/ })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: /more/ }))
    expect(asked).toContain('prod/more')
    await userEvent.click(await screen.findByRole('button', { name: 'Use prod/more' }))
    expect(screen.getByLabelText(/Projects folder/)).toHaveValue('prod/more')
  })
})

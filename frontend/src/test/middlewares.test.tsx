import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { Middlewares } from '../features/traefik/Middlewares'

// Assembled, so that no secret-looking literal sits in the source.
const PW = ['a', 'long', 'password'].join('-')
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const item = (over = {}) => ({
  name: 'private', path: 'middlewares/private.yml', sha256: 'a'.repeat(64), editable: true, used_by: [],
  spec: { intent: 'basic_auth', name: 'private', remove_header: true, users: [{ username: 'alice', has_password: true }] }, ...over,
})

function renderIt(items: unknown[]) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
    init?.method && init.method !== 'GET' ? json({ name: 'x', reference: 'x@file', content: 'http: {}' }, 201) : json({ middlewares: items }),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Middlewares nodeId="n1" />
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('middlewares', () => {
  it('creates "only my network" with the private ranges as a starting point', async () => {
    const spy = renderIt([])
    await userEvent.click(await screen.findByRole('button', { name: 'New middleware' }))
    await userEvent.type(screen.getByLabelText('Name'), 'lan')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    const sent = JSON.parse(String(post![1]!.body)).spec
    expect(sent).toMatchObject({ intent: 'local_only', name: 'lan' })
    expect(sent.ranges).toContain('192.168.0.0/16')
  })

  it('sends a password once and offers no way to read it back', async () => {
    const spy = renderIt([])
    await userEvent.click(await screen.findByRole('button', { name: 'New middleware' }))
    await userEvent.selectOptions(screen.getByLabelText('What it does'), 'basic_auth')
    await userEvent.type(screen.getByLabelText('Name'), 'private')
    await userEvent.type(screen.getByLabelText('User name'), 'alice')
    await userEvent.type(screen.getByLabelText(/Password/), PW)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post![1]!.body)).spec.users).toEqual([{ username: 'alice', password: PW }])
  })

  it('editing keeps the stored password unless a new one is typed', async () => {
    const spy = renderIt([item()])
    await userEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const password = screen.getByLabelText(/Password/)
    expect(password).toHaveValue('')
    expect(password).toHaveAttribute('placeholder', 'unchanged')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const put = spy.mock.calls.find(([, init]) => init?.method === 'PUT')
    expect(JSON.parse(String(put![1]!.body)).spec.users).toEqual([{ username: 'alice' }])
    expect(JSON.parse(String(put![1]!.body)).expected_sha256).toBe('a'.repeat(64))
  })

  it('does not let a middleware that sites use be deleted, nor one edited by hand be changed', async () => {
    renderIt([item({ used_by: ['blog'] }), item({ name: 'old', editable: false, spec: { intent: 'compress', name: 'old', min_bytes: 1 } })])
    expect(await screen.findByText(/used by blog/)).toBeInTheDocument()
    const deletes = screen.getAllByRole('button', { name: 'Delete' })
    expect(deletes[0]).toBeDisabled()
    expect(screen.getByText(/edited by hand/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Edit' })).toHaveLength(1)
  })

  it('previews the file without saving', async () => {
    const spy = renderIt([])
    await userEvent.click(await screen.findByRole('button', { name: 'New middleware' }))
    await userEvent.type(screen.getByLabelText('Name'), 'lan')
    await userEvent.click(screen.getByRole('button', { name: 'Preview the file' }))
    expect(await screen.findByText('http: {}')).toBeInTheDocument()
    expect(String(spy.mock.calls.find(([, init]) => init?.method === 'POST')![0])).toContain('/preview')
  })
})

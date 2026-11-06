import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ManagedSites } from '../features/traefik/ManagedSites'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const spec = {
  name: 'blog', hosts: ['blog.example.com'], path_prefix: null, container: 'blog-web', url: null,
  port: 80, scheme: 'http', middlewares: [], https: true, cert_resolver: null, entrypoints: null,
}
let dnsStatus = 'missing'
const calls: { method: string; path: string; body: unknown }[] = []

function server(sites: unknown[]) {
  calls.length = 0
  dnsStatus = 'missing'
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : null })
    if (path.endsWith('/dns/report')) return json({ hosts: [{ host: 'blog.example.com', status: dnsStatus }] })
    if (path.endsWith('/dns/fix')) { dnsStatus = 'present'; return json({}, 201) }
    if (path.endsWith('/certs')) return json({ hosts: [{ host: 'blog.example.com', state: 'missing' }] })
    if (path.endsWith('/managed')) return json({ sites })
    if (path.endsWith('/traefik/sites')) return method === 'POST' ? json({ name: 'blog' }, 201) : json({ middlewares: [{ id: 'auth@file' }] })
    if (path.endsWith('/sites/preview')) return json({ yaml: 'http:\n  routers: {}\n', name_taken: false })
    if (path.includes('/docker/containers')) return json({ containers: [{ names: ['blog-web'], ports: [{ private: 8080 }] }] })
    if (path.includes('/history/')) return json({ versions: [{ version: '1700000000000.abcd1234', at: 1700000000000, size: 10 }] })
    return json({ done: true })
  })
}

function renderIt() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <ManagedSites nodeId="n1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('site builder', () => {
  it('builds a site from the form and fills the port from the chosen container', async () => {
    server([])
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: 'New site' }))
    await userEvent.type(screen.getByLabelText(/^Name/), 'blog')
    await userEvent.type(screen.getByLabelText('Address'), 'blog.example.com')
    await userEvent.type(screen.getByLabelText('Container name'), 'blog-web')
    expect(screen.getByLabelText('Port')).toHaveValue(8080)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    // After saving, the flow goes on with DNS and certificate instead of dropping back to the list.
    expect(await screen.findByText('Your new site')).toBeInTheDocument()
    expect(await screen.findByText('Missing')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Create the record' }))
    expect(await screen.findByText('In the DNS')).toBeInTheDocument()
    expect(screen.getByText('No certificate')).toBeInTheDocument()
    expect(calls.map((c) => c.path)).toEqual(['/nodes/n1/traefik/sites', '/nodes/n1/dns/fix'])
    expect(calls[1].body).toEqual({ host: 'blog.example.com' })
    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(await screen.findByRole('button', { name: 'New site' })).toBeInTheDocument()
    expect(calls[0].path).toBe('/nodes/n1/traefik/sites')
    expect(calls[0].body).toMatchObject({ spec: { name: 'blog', hosts: ['blog.example.com'], container: 'blog-web', port: 8080, https: true, url: null } })
  })

  it('pauses, asks twice before deleting, and offers older versions', async () => {
    server([{ name: 'blog', paused: false, sha256: 'a'.repeat(64), spec, modified_by_hand: false }])
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: 'Pause' }))
    expect(calls.at(-1)).toMatchObject({ method: 'POST', path: '/nodes/n1/traefik/sites/blog/pause' })
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Confirm deletion' }))
    expect(calls.at(-1)?.path).toBe(`/nodes/n1/traefik/sites/blog?expected_sha256=${'a'.repeat(64)}`)
    await userEvent.click(screen.getByRole('button', { name: 'History' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Put back' }))
    expect(calls.at(-1)).toMatchObject({ path: '/nodes/n1/traefik/restore/sites/blog.yml', body: { version: '1700000000000.abcd1234' } })
  })

  it('does not offer to edit a site that was changed by hand, and offers resume when paused', async () => {
    server([
      { name: 'hand', paused: false, sha256: 'b'.repeat(64), spec, modified_by_hand: true },
      { name: 'old', paused: true, sha256: 'c'.repeat(64), spec, modified_by_hand: false },
    ])
    renderIt()
    expect(await screen.findByText(/edited by hand/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument()
  })
})

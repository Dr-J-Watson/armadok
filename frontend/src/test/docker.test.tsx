import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ContainersTab } from '../features/docker/ContainersTab'
import { ResourcesTab } from '../features/docker/ResourcesTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const container = (id: string, name: string, state: string) => ({
  id: id.repeat(12),
  names: [name],
  image: 'nginx:1',
  state,
  status: state,
  networks: [],
  ports: [],
})

function wrap(node: React.ReactNode) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('containers', () => {
  it('offers the actions that fit the state, asks twice before removing, and shows logs', async () => {
    const posts: { path: string; body: unknown }[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input).replace('/api/v1', '')
      if (init?.method === 'POST') {
        posts.push({ path, body: JSON.parse(String(init.body)) })
        return json({ done: true })
      }
      if (path.includes('/logs')) return json({ output: 'hello from web' })
      return json({ containers: [container('a', 'web', 'running'), container('b', 'old', 'exited')], truncated: false })
    })
    wrap(<ContainersTab nodeId="n1" />)
    expect(await screen.findByText('old')).toBeInTheDocument()
    // A stopped container cannot be stopped again; a running one cannot be removed.
    expect(screen.getAllByRole('button', { name: 'Stop' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(1)

    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(posts).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Confirm removal' }))
    expect(posts).toEqual([{ path: `/nodes/n1/docker/containers/${'b'.repeat(12)}/action`, body: { action: 'remove' } }])

    await userEvent.click(screen.getAllByRole('button', { name: 'Logs' })[0])
    expect(await screen.findByText('hello from web')).toBeInTheDocument()
  })

  it('shows the error of a refused action', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) =>
      init?.method === 'POST'
        ? json({ error: { code: 'rights.missing' } }, 403)
        : json({ containers: [container('a', 'web', 'running')], truncated: false }),
    )
    wrap(<ContainersTab nodeId="n1" />)
    await userEvent.click(await screen.findByRole('button', { name: 'Restart' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})

describe('resources', () => {
  it('lists images, networks and volumes', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input)
      if (path.endsWith('/images')) return json({ items: [{ id: 'sha256:' + 'c'.repeat(64), tags: ['nginx:1'], size: 5242880 }] })
      if (path.endsWith('/networks')) return json({ items: [{ id: 'd', name: 'demo_default', driver: 'bridge' }] })
      return json({ items: [] })
    })
    wrap(<ResourcesTab nodeId="n1" />)
    expect(await screen.findByText('nginx:1')).toBeInTheDocument()
    expect(await screen.findByText('demo_default')).toBeInTheDocument()
    expect(await screen.findByText('None.')).toBeInTheDocument()
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { MapTab } from '../features/relations/MapTab'

// The canvas cannot run without a browser; the list and the panel are what is tested here.
vi.mock('../features/relations/GraphView', () => ({ GraphView: () => <div data-testid="canvas" /> }))

const objects = [
  { type: 'container', id: 'c1', label: 'web' },
  { type: 'image', id: 'nginx:1.27', label: 'nginx:1.27' },
  { type: 'router', id: 'web', label: 'web' },
]
const links = [
  { source: { type: 'container', id: 'c1' }, target: { type: 'image', id: 'nginx:1.27' }, kind: 'runs' },
  { source: { type: 'router', id: 'web' }, target: { type: 'container', id: 'c1' }, kind: 'routes_to' },
]

function serve(overrides: Record<string, unknown> = {}) {
  const calls: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const path = String(input).replace('/api/v1', '')
    calls.push(path)
    if (path === '/nodes/n1/relations') return json({ objects, links, unavailable: {}, ...overrides })
    if (path === '/nodes/n1/relations/container/c1')
      return json({
        object: objects[0],
        links: [
          { direction: 'outgoing', kind: 'runs', object: objects[1] },
          { direction: 'incoming', kind: 'routes_to', object: objects[2] },
        ],
      })
    if (path === '/nodes/n1/relations/image/nginx%3A1.27')
      return json({ object: objects[1], links: [{ direction: 'incoming', kind: 'runs', object: objects[0] }] })
    return json({ error: { code: 'common.not_found' } }, 404)
  })
  return calls
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

function renderMap(url = '/nodes/n1?tab=map') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[url]}>
        <MapTab nodeId="n1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('relations map', () => {
  it('shows links in both directions for the selected object and follows a neighbour', async () => {
    const calls = serve()
    renderMap()
    await userEvent.click(await screen.findByText('Text list of objects'))
    const web = (await screen.findAllByRole('button', { name: 'web' }))[0] // the container
    await userEvent.click(web)
    expect(await screen.findByText(/points to · runs/)).toBeInTheDocument()
    expect(screen.getByText(/pointed at by · routes to/)).toBeInTheDocument()
    // Following the neighbour (the image) loads its own panel, where the link reads the other way.
    const panel = within(screen.getByRole('complementary'))
    await userEvent.click(panel.getByRole('button', { name: 'nginx:1.27' }))
    expect(await screen.findByText(/pointed at by · runs/)).toBeInTheDocument()
    expect(calls).toContain('/nodes/n1/relations/image/nginx%3A1.27')
  })

  it('opens on the object named in the address and offers the Containers screen', async () => {
    serve()
    renderMap('/nodes/n1?tab=map&focus=container%3Ac1')
    expect(await screen.findByRole('link', { name: 'Open in Containers' })).toHaveAttribute(
      'href',
      '/nodes/n1?tab=containers',
    )
  })

  it('hides a type from the list and remembers it for this node only', async () => {
    serve()
    const { unmount } = renderMap()
    await userEvent.click(await screen.findByText('Text list of objects'))
    expect(await screen.findByRole('button', { name: 'nginx:1.27' })).toBeInTheDocument()
    await userEvent.click(await screen.findByRole('checkbox', { name: 'Image' }))
    expect(screen.queryByRole('button', { name: 'nginx:1.27' })).not.toBeInTheDocument()
    unmount()
    renderMap()
    expect(await screen.findByRole('checkbox', { name: 'Image' })).not.toBeChecked()
    expect(window.localStorage.getItem('armadok.map.hidden.n1')).toContain('image')
    expect(window.localStorage.getItem('armadok.map.hidden.other')).toBeNull()
  })

  it('says why a provider is missing instead of failing, and shows an empty state', async () => {
    serve({ objects: [], links: [], unavailable: { docker: 'worker.not_connected' } })
    renderMap()
    expect(await screen.findByText(/Some relations could not be loaded \(Docker\)/)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('No worker is connected for this node.')
    expect(screen.getByText(/Nothing to show yet/)).toBeInTheDocument()
  })

  it('offers a text search over the objects', async () => {
    serve()
    renderMap()
    await userEvent.type(await screen.findByRole('searchbox'), 'nginx')
    expect(screen.getByRole('button', { name: 'nginx:1.27' })).toBeInTheDocument()
  })
})

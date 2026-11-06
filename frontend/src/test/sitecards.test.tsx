import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { MemoryRouter } from 'react-router-dom'
import { SitesPage } from '../features/traefik/SitesPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const site = (name: string, host: string, over = {}) => ({
  name, paused: false, sha256: 'a'.repeat(64), modified_by_hand: false,
  spec: { name, hosts: [host], path_prefix: null, container: `${name}-web`, url: null, port: 80, scheme: 'http', middlewares: ['lan@file'], https: true, cert_resolver: null, entrypoints: null, ...over },
})

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('site cards', () => {
  it('shows each site as a card with its address, target and middleware pills, and searches', async () => {
    const blog = site('blog', 'blog.example.com')
    const shop = site('shop', 'shop.example.org', { middlewares: [] })
    const router = (s: typeof blog) => ({
      id: `${s.name}@file`, name: s.name, rule: `Host(\`${s.spec.hosts[0]}\`)`, service: `${s.name}@file`,
      middlewares: s.spec.middlewares, tls: true, path: `sites/${s.name}.yml`,
    })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const path = String(input)
      if (path.endsWith('/managed')) return json({ sites: [blog, shop] })
      if (path.endsWith('/traefik/items/overview'))
        return json({
          routers: [router(blog), router(shop)],
          services: [{ id: 'blog@file', urls: ['http://blog-web:80'] }, { id: 'shop@file', urls: ['http://shop-web:80'] }],
          middlewares: [], errors: [],
        })
      return json({ error: { code: 'common.not_found' } }, 404)
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <SitesPage nodeId="n1" />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const link = (await screen.findAllByRole('link', { name: 'Open the site' }))[0]
    expect(link).toHaveAttribute('href', 'https://blog.example.com')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener')) // an address from a file never gets the opener
    expect(screen.getAllByText('http://blog-web:80').length).toBeGreaterThan(0)
    expect(screen.getByText('lan')).toBeInTheDocument() // the middleware, without its "@file"
    expect(screen.getByText('No protection')).toBeInTheDocument() // the shop has none
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search a site' }), 'example.org')
    expect(screen.queryByText('blog.example.com')).toBeNull()
    expect(screen.getByText('shop.example.org')).toBeInTheDocument()
  })
})

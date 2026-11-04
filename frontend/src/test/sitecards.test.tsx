import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ManagedSites } from '../features/traefik/ManagedSites'

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
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ sites: [site('blog', 'blog.example.com'), site('shop', 'shop.example.org', { middlewares: [] })] }),
    )
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ManagedSites nodeId="n1" />
      </QueryClientProvider>,
    )
    const link = await screen.findByRole('link', { name: 'blog.example.com' })
    expect(link).toHaveAttribute('href', 'https://blog.example.com')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener')) // an address from a file never gets the opener
    expect(screen.getByText('blog-web:80')).toBeInTheDocument()
    expect(screen.getByText('lan')).toBeInTheDocument() // the middleware, without its "@file"
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search a site' }), 'example.org')
    expect(screen.queryByRole('link', { name: 'blog.example.com' })).toBeNull()
    expect(screen.getByRole('link', { name: 'shop.example.org' })).toBeInTheDocument()
  })
})

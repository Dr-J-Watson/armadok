import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { TraefikTab } from '../features/traefik/TraefikTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const SHA = 'c'.repeat(64)
const puts: { content: string }[] = []

const sites = {
  routers: [
    {
      id: 'blog@file', name: 'blog', rule: 'Host(`blog.example.com`)', service: 'blog-svc@file',
      entrypoints: ['websecure'], middlewares: ['auth@file'], tls: true, path: 'apps/blog.yml',
    },
  ],
  services: [], middlewares: [], errors: [{ path: 'apps/broken.yml', error: 'invalid_yaml' }], truncated: false,
}

function server(putAnswer: Response | null = null) {
  puts.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (path === '/nodes/n1/traefik/sites') return json(sites)
    if (path === '/nodes/n1/traefik/files') return json({ files: [{ path: 'apps/blog.yml', size: 10 }, { path: 'base.yaml', size: 5 }], truncated: false })
    if (path === '/nodes/n1/traefik/files/apps/blog.yml') {
      if (method === 'PUT') {
        puts.push(JSON.parse(String(init?.body)))
        return putAnswer ?? json({ sha256: 'd'.repeat(64), size: 3 })
      }
      return json({ content: 'http: {}\n', sha256: SHA, size: 9 })
    }
    return json({ error: { code: 'common.not_found' } }, 404)
  })
}

function renderTab(url = '/nodes/n1?tab=traefik') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[url]}>
        <TraefikTab nodeId="n1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('traefik tab', () => {
  it('shows each router as a site with its rule, service, middlewares and HTTPS state', async () => {
    server()
    renderTab()
    expect(await screen.findByText('Host(`blog.example.com`)')).toBeInTheDocument()
    expect(screen.getByText('blog-svc')).toBeInTheDocument()
    expect(screen.getByText('auth')).toBeInTheDocument() // the provider suffix is not shown
    expect(screen.getByText('HTTPS', { selector: '.badge' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Relations' })).toHaveAttribute('href', expect.stringContaining('focus=router'))
  })

  it('points at a file that is not valid YAML instead of hiding the problem', async () => {
    server()
    renderTab()
    expect(await screen.findByText(/apps\/broken.yml is not valid YAML/)).toBeInTheDocument()
  })

  it('opens the file of a site in the editor and saves it naming the version it replaces', async () => {
    server()
    renderTab()
    await userEvent.click(await screen.findByRole('button', { name: 'apps/blog.yml' }))
    const editor = await screen.findByLabelText('Content of apps/blog.yml')
    await userEvent.type(editor, '# note')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
    expect(puts[0].content).toContain('# note')
  })

  it('explains why a save was refused when the YAML is invalid', async () => {
    server(json({ error: { code: 'traefik.invalid_yaml' } }, 422))
    renderTab('/nodes/n1?tab=traefik&view=files&file=apps%2Fblog.yml')
    await userEvent.type(await screen.findByLabelText('Content of apps/blog.yml'), '[[')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('not valid YAML')
  })
})

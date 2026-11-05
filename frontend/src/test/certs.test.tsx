import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { CertsTab } from '../features/certs/CertsTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

function renderTab(answer: unknown, status = 200) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(answer, status))
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <CertsTab nodeId="n1" />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

const cert = (over = {}) => ({
  resolver: 'letsencrypt', names: ['blog.example.com'], issuer: "Let's Encrypt / R10", not_before: '2026-01-01T00:00:00Z',
  not_after: '2027-01-01T00:00:00Z', serial: 'ab12', days_left: 80, state: 'ok', ...over,
})

describe('certificates tab', () => {
  it('lists certificates with the days left, and flags the sites that have a problem', async () => {
    renderTab({
      certificates: [cert(), cert({ names: ['*.example.com', 'example.com'], serial: 'cd34', days_left: 1, state: 'expiring' }), cert({ names: ['old.example.org'], serial: 'ef56', days_left: -3, state: 'expired' })],
      hosts: [
        { host: 'blog.example.com', serial: 'ab12', state: 'ok' },
        { host: 'old.example.org', serial: 'ef56', state: 'expired' },
        { host: 'nocert.example.net', serial: null, state: 'missing' },
      ],
      truncated: false,
    })
    expect(await screen.findByText('80 days left')).toBeInTheDocument()
    expect(screen.getByText('1 day left')).toBeInTheDocument() // singular
    expect(screen.getAllByText('Expired').length).toBeGreaterThan(0)
    const problems = screen.getByRole('region', { name: 'Sites with a certificate problem' })
    expect(problems).toHaveTextContent('nocert.example.net')
    expect(problems).toHaveTextContent('old.example.org')
    expect(problems).not.toHaveTextContent('blog.example.com') // the healthy one is not a problem
  })

  it('says why the certificates cannot be read instead of failing silently', async () => {
    renderTab({ error: { code: 'worker.acme_not_configured' } }, 409)
    expect(await screen.findByRole('status')).toHaveTextContent('No acme.json is configured')
  })

  it('shows an empty state and never offers anything that could hold a key', async () => {
    const { container } = renderTab({ certificates: [], hosts: [], truncated: false })
    expect(await screen.findByText('No certificate found.')).toBeInTheDocument()
    expect(container.textContent).toMatch(/private keys never leave/)
  })
})

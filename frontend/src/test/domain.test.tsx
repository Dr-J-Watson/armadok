import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { DomainSwitch } from '../features/traefik/DomainSwitch'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('pausing a domain', () => {
  it('shows the plan with the shared sites, and applies only after the second click', async () => {
    const sent: { path: string; body: Record<string, unknown> }[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      sent.push({ path: String(input), body: JSON.parse(String(init?.body)) })
      return json({ sites: ['blog', 'shop'], shared: ['mixed'] })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <DomainSwitch nodeId="n1" />
      </QueryClientProvider>,
    )
    await userEvent.type(screen.getByLabelText('Domain (for example example.com)'), 'Example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Show what will change' }))
    expect(await screen.findByText('2 site(s): blog, shop.')).toBeInTheDocument()
    expect(screen.getByText(/Left alone because they also answer for another domain: mixed/)).toBeInTheDocument()
    expect(sent).toHaveLength(1)
    expect(sent[0]).toEqual({ path: '/api/v1/nodes/n1/traefik/domains/example.com/pause', body: { dry_run: true } })
    await userEvent.click(screen.getByRole('button', { name: 'Pause these sites' }))
    await vi.waitFor(() => expect(sent).toHaveLength(2))
    expect(sent[1].body).toEqual({})
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { VisitorsPanel } from '../features/traffic/VisitorsPanel'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})
const wrap = (ui: React.ReactNode) =>
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>)

const answer = (extra: object = {}) => ({
  masked: true,
  visitors: [{ ip: '192.0.2.0/24', requests: 3, errors: 1, bytes: 10, first_seen: '2026-11-05T10:00:00Z', last_seen: '2026-11-05T11:00:00Z' }],
  paths: [{ path: '/a', requests: 3, errors: 1 }],
  classes: { '2xx': 2, '4xx': 1 },
  kept: 3,
  reading: true,
  ...extra,
})

describe('visitors', () => {
  it('shows networks, and says the addresses are masked, when the person may not see them whole', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(answer()))
    wrap(<VisitorsPanel nodeId="n1" />)
    expect(await screen.findByText('192.0.2.0/24')).toBeInTheDocument()
    expect(screen.getByText(/grouped by network/i)).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Network' })).toBeInTheDocument()
  })

  it('shows whole addresses when allowed', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json(answer({ masked: false, visitors: [{ ip: '192.0.2.7', requests: 1, errors: 0, bytes: 1, first_seen: '2026-11-05T10:00:00Z', last_seen: '2026-11-05T10:00:00Z' }] })),
    )
    wrap(<VisitorsPanel nodeId="n1" />)
    expect(await screen.findByText('192.0.2.7')).toBeInTheDocument()
    expect(screen.queryByText(/grouped by network/i)).toBeNull()
  })

  it('explains how to turn the access log on when nothing is read yet', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(answer({ reading: false, visitors: [], paths: [], kept: 0 })))
    wrap(<VisitorsPanel nodeId="n1" />)
    expect(await screen.findByText(/ARMADOK_TRAEFIK_ACCESSLOG/)).toBeInTheDocument()
  })

  it('is absent for someone who may not read the log', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ error: { code: 'rights.missing' } }, 403))
    const { container } = wrap(<VisitorsPanel nodeId="n1" />)
    await vi.waitFor(() => expect(container).toBeEmptyDOMElement())
  })

  it('deletes the kept entries only after confirmation', async () => {
    const calls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      calls.push(`${init?.method ?? 'GET'} ${String(input).replace('/api/v1', '').split('?')[0]}`)
      return json(init?.method === 'DELETE' ? { removed: 3 } : answer())
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    wrap(<VisitorsPanel nodeId="n1" />)
    const button = await screen.findByRole('button', { name: 'Delete the kept entries' })
    await userEvent.click(button)
    expect(calls).not.toContain('DELETE /nodes/n1/traffic/log')
    await userEvent.click(button)
    await vi.waitFor(() => expect(calls).toContain('DELETE /nodes/n1/traffic/log'))
    expect(confirm).toHaveBeenCalledTimes(2)
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { TrafficTab } from '../features/traffic/TrafficTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const wrap = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TrafficTab nodeId="n1" />
    </QueryClientProvider>,
  )

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('traffic', () => {
  it('shows each service with its error share, response times and a trend, and changes the range', async () => {
    const asked: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      asked.push(String(input))
      return json({
        range: '24h',
        status: { last_ok: '2026-11-04T10:00:00+00:00', last_error: null, reload_ok: true, servers: {} },
        services: [
          { service: 'blog@file', requests: 100, per_second: 0.001, errors_5xx: 10, errors_4xx: 5, error_rate: 0.1, previous_requests: 50, bytes_in: 2048, bytes_out: 2097152, p50: 0.05, p95: 0.4, p99: 1.5, series: [[40, 4], [60, 6]] },
        ],
      })
    })
    wrap()
    expect(await screen.findByText('blog')).toBeInTheDocument()
    expect(screen.getByText(/10\.0 %/)).toBeInTheDocument()
    expect(screen.getByText(/p95 400 ms/)).toBeInTheDocument()
    expect(screen.getByText(/\+100 %/)).toBeInTheDocument() // against the previous period
    expect(screen.getByRole('img', { name: 'Requests over time for blog@file' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '7 days' }))
    await vi.waitFor(() => expect(asked.some((a) => a.endsWith('range=7d'))).toBe(true))
  })

  it('walks through the set-up when the machine has no metrics yet, and never lists a public port', async () => {
    // A fresh response for every call: the page asks for the figures and for the visitors.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      json({ range: '24h', services: [], status: { last_ok: null, last_error: 'worker.metrics_not_configured', reload_ok: null, servers: {} } }),
    )
    wrap()
    expect(await screen.findByText('Turn on the statistics')).toBeInTheDocument()
    expect(screen.getByText(/entryPoint: metrics/)).toBeInTheDocument()
    expect(screen.getByText(/must NOT be published/)).toBeInTheDocument()
  })
})

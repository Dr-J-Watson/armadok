import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { MachineTab } from '../features/machine/MachineTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const stats = (over = {}) => ({
  cpu_percent: 12.5, cpu_count: 4, load: [0.5, 0.4, 0.3], memory_total: 8 * 1073741824, memory_available: 6 * 1073741824,
  swap_total: 0, swap_free: 0, uptime_seconds: 90061, network_received: 1073741824, network_sent: 0, temperature: 51.5,
  disks: [{ name: 'projects', total: 100 * 1073741824, free: 40 * 1073741824 }], ...over,
})

function renderTab(answer: unknown, status = 200) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(answer, status))
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MachineTab nodeId="n1" />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('machine tab', () => {
  it('shows processor, memory, disks, uptime and temperature', async () => {
    renderTab(stats())
    expect(await screen.findByText(/12.5%/)).toBeInTheDocument()
    expect(screen.getByText(/2.0 GiB \/ 8.0 GiB/)).toBeInTheDocument()
    expect(screen.getByText(/40.0 GiB free of 100.0 GiB/)).toBeInTheDocument()
    expect(screen.getByText(/1 d 1 h/)).toBeInTheDocument()
    expect(screen.getByText(/51.5 °C/)).toBeInTheDocument()
    expect(screen.queryByText('Swap')).toBeNull() // no swap, no line
  })

  it('leaves out the temperature when the machine has no sensor', async () => {
    renderTab(stats({ temperature: null }))
    await screen.findByText(/12.5%/)
    expect(screen.queryByText(/°C/)).toBeNull()
  })

  it('says why the figures cannot be read', async () => {
    renderTab({ error: { code: 'worker.not_connected' } }, 409)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})

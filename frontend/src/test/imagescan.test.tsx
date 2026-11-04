import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ImageScanTab } from '../features/imagescan/ImageScanTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const scan = (over = {}) => ({
  image_id: 'sha256:' + 'a'.repeat(64), name: 'web:1', os: 'debian', os_version: '12', packages: 120,
  scanned_at: '2030-01-01T00:00:00Z', status: 'ok', counts: { high: 1 },
  findings: [{ component: 'openssl', version: '3.0', id: 'DSA-1', severity: 'high', summary: 'bad thing', fixed: '3.1' }],
  ...over,
})

function renderTab(answer: unknown) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
    init?.method === 'POST' ? json({ results: [] }) : json(answer),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ImageScanTab nodeId="n1" />
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('image scan tab', () => {
  it('shows each image with its flaws and fixed versions', async () => {
    renderTab({ enabled: true, scans: [scan()], can_run: true })
    expect(await screen.findByText('web:1')).toBeInTheDocument()
    expect(screen.getByText(/High 1/)).toBeInTheDocument()
    expect(screen.getByText(/fixed in 3.1/)).toBeInTheDocument()
  })

  it('says scanning is off and offers nothing to scan', async () => {
    renderTab({ enabled: false, scans: [], can_run: true })
    expect(await screen.findByText(/switched off on this server/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Scan the images in use' })).toBeNull()
  })

  it('scans the images in use on request', async () => {
    const spy = renderTab({ enabled: true, scans: [], can_run: true })
    await userEvent.click(await screen.findByRole('button', { name: 'Scan the images in use' }))
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(String(post![0])).toContain('/nodes/n1/imagescan/scan')
  })
})

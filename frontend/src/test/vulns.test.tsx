import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { VulnsPage } from '../features/vulns/VulnsPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const finding = (over = {}) => ({
  id: 1, component: 'jinja2', version: '2.4.1', vuln_id: 'GHSA-aaaa-bbbb-cccc', severity: 'high', summary: 'Sandbox escape',
  fixed: '2.10.1', aliases: ['CVE-2019-10906'], first_seen: '2026-01-01T00:00:00Z', ignored_reason: null, ...over,
})
const answer = (over = {}) => ({
  enabled: true, scan: { finished_at: '2026-01-01T00:00:00Z', ok: true, error: null, components: 24 }, findings: [finding()], can_manage: true, ...over,
})

function renderPage(body: unknown) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
    init?.method && init.method !== 'GET' ? json({ announced: 2, ignored: true }) : json(body),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <VulnsPage />
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('vulnerabilities page', () => {
  it('lists each finding with its severity, its fix and its identifiers', async () => {
    renderPage(answer())
    expect(await screen.findByText(/Sandbox escape/)).toBeInTheDocument()
    expect(screen.getByText('High')).toBeInTheDocument()
    expect(screen.getByText(/Fixed in 2.10.1/)).toBeInTheDocument()
    expect(screen.getByText(/CVE-2019-10906/)).toBeInTheDocument()
  })

  it('says the check is off and offers no button when it is', async () => {
    renderPage(answer({ enabled: false, scan: null, findings: [] }))
    expect(await screen.findByText(/The check is off/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull()
  })

  it('requires a reason to accept a finding', async () => {
    const spy = renderPage(answer())
    await userEvent.click(await screen.findByRole('button', { name: 'Accept for now' }))
    await userEvent.type(screen.getByLabelText(/Why/), 'not reachable here')
    await userEvent.click(screen.getAllByRole('button', { name: 'Accept for now' }).at(-1)!)
    const put = spy.mock.calls.find(([, init]) => init?.method === 'PUT')
    expect(String(put![0])).toContain('/vulns/1/ignore')
    expect(JSON.parse(String(put![1]!.body))).toEqual({ reason: 'not reachable here' })
  })

  it('shows an accepted finding with its reason and can stop accepting it', async () => {
    renderPage(answer({ findings: [finding({ ignored_reason: 'not reachable' })] }))
    expect(await screen.findByText(/Accepted for now: not reachable/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop accepting' })).toBeInTheDocument()
  })

  it('hides the controls from someone who can only read', async () => {
    renderPage(answer({ can_manage: false }))
    await screen.findByText(/Sandbox escape/)
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Accept for now' })).toBeNull()
  })
})

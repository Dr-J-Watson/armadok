import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { StateBackupsPage } from '../features/statebackup/StateBackupsPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const answer = (over = {}) => ({
  backups: [{ name: 'armadok-state-20260101T031500Z.tar.gz.enc', stamp: '20260101T031500Z', size: 2048 }],
  keep: 7, every_hours: 24, ready: true, custom_folder: false, ...over,
})

function renderPage(body: unknown) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) =>
    init?.method && init.method !== 'GET' ? json({ name: 'x' }, 201) : json(body),
  )
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <StateBackupsPage />
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('state backups page', () => {
  it('says how often backups are made and warns when they sit on the data disk', async () => {
    renderPage(answer())
    expect(await screen.findByText(/every 24 hours/)).toBeInTheDocument()
    expect(screen.getByText(/stored next to the data/)).toBeInTheDocument()
    expect(screen.getByText(/2 KiB/)).toBeInTheDocument()
  })

  it('refuses to offer a backup without an encryption key', async () => {
    renderPage(answer({ ready: false }))
    expect(await screen.findByText(/No encryption key is set/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Back up now' })).toBeDisabled()
  })

  it('asks for a fresh code before a download', async () => {
    const spy = renderPage(answer({ custom_folder: true }))
    await userEvent.click(await screen.findByRole('button', { name: 'Download' }))
    expect(await screen.findByLabelText('Your authenticator code')).toBeInTheDocument()
    expect(spy.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
  })

  it('makes a backup on demand', async () => {
    const spy = renderPage(answer({ custom_folder: true }))
    await userEvent.click(await screen.findByRole('button', { name: 'Back up now' }))
    expect(spy.mock.calls.some(([url, init]) => init?.method === 'POST' && String(url).endsWith('/statebackups'))).toBe(true)
  })
})

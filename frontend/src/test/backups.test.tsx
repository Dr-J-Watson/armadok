import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { BackupsTab } from '../features/backups/BackupsTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const archive = { name: 'data--20260101T031500Z.tar.gz', volume: 'data', stamp: '20260101T031500Z', size: 5 * 1048576 }
const overview = (over = {}) => ({
  configured: true, archives: [archive], jobs: [{ id: 'j1', kind: 'backup', volume: 'data', archive: archive.name, size: 1, state: 'success', error: null, created_at: '2026-01-01T03:15:00Z' }],
  can_create: true, can_manage: true, default_keep: 7, ...over,
})

function renderTab(answer: unknown) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (init?.method && init.method !== 'GET') return json({ job: 'j2' }, 202)
    if (String(url).endsWith('/docker/volumes')) return json({ items: [{ name: 'data', driver: 'local' }, { name: 'nfs', driver: 'nfs' }] })
    return json(answer)
  })
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <BackupsTab nodeId="n1" />
    </QueryClientProvider>,
  )
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('backups tab', () => {
  it('says how to turn backups on when the machine has no folder', async () => {
    renderTab(overview({ configured: false, archives: [] }))
    expect(await screen.findByText(/no backup folder/)).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Back up a volume' })).toBeNull()
  })

  it('lists archives with size and offers only local volumes', async () => {
    renderTab(overview())
    expect(await screen.findByText(/5.0 MiB/)).toBeInTheDocument()
    expect(await screen.findByRole('option', { name: 'data' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'nfs' })).toBeNull()
  })

  it('starts a backup with the number of archives to keep', async () => {
    const spy = renderTab(overview())
    await screen.findByRole('option', { name: 'data' })
    await userEvent.selectOptions(screen.getByLabelText('Volume'), 'data')
    await userEvent.click(screen.getByRole('button', { name: 'Back up now' }))
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(String(post![0])).toContain('/backups/volumes/data')
    expect(JSON.parse(String(post![1]!.body))).toEqual({ keep: 7 })
  })

  it('only restores once the volume name is typed again', async () => {
    const spy = renderTab(overview())
    await userEvent.click(await screen.findByRole('button', { name: 'Restore' }))
    const confirm = await screen.findByLabelText('Type the volume name to confirm')
    const submit = screen.getAllByRole('button', { name: 'Restore' }).at(-1)!
    expect(submit).toBeDisabled()
    await userEvent.type(confirm, 'data')
    expect(submit).toBeEnabled()
    await userEvent.click(submit)
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post![1]!.body))).toEqual({ archive: archive.name, volume: 'data', confirm: 'data', other: false, clear: false })
  })

  it('can empty the volume first, and offers a download only to those who may', async () => {
    const spy = renderTab(overview({ can_download: true }))
    expect(await screen.findByRole('button', { name: 'Download' })).toBeInTheDocument()
    await userEvent.click(await screen.findByRole('button', { name: 'Restore' }))
    await userEvent.click(await screen.findByLabelText('Empty the volume first'))
    expect(screen.getByText(/deleted before the archive is restored/)).toBeInTheDocument()
    await userEvent.type(await screen.findByLabelText('Type the volume name to confirm'), 'data')
    await userEvent.click(screen.getAllByRole('button', { name: 'Restore' }).at(-1)!)
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post![1]!.body)).clear).toBe(true)
  })

  it('hides restore and delete from those who cannot manage', async () => {
    renderTab(overview({ can_manage: false, can_create: false }))
    await screen.findByText(/5.0 MiB/)
    expect(screen.queryByRole('button', { name: 'Restore' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
  })
})

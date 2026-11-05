import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ImageUpdatesTab } from '../features/imageupdates/ImageUpdatesTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('image updates', () => {
  it('lists each image with its state, and checks on request', async () => {
    let checked = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
      if ((init?.method ?? 'GET') === 'POST') {
        checked = true
        return json({ counts: {} })
      }
      return json({
        can_check: true,
        images: [
          { ref: 'redis:7', state: 'update', in_use: true, remote_digest: 'sha256:x', error: null, checked_at: '2026-11-04T10:00:00+00:00' },
          { ref: 'local/app:dev', state: 'unknown', in_use: true, remote_digest: null, error: 'images.no_digest', checked_at: '2026-11-04T10:00:00+00:00' },
        ],
      })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ImageUpdatesTab nodeId="n1" />
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Update available')).toBeInTheDocument()
    expect(screen.getByText(/there is no registry version to compare with/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Check now' }))
    await vi.waitFor(() => expect(checked).toBe(true))
  })

  it('offers no check button to someone who may only read', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) =>
      String(input).endsWith('/registries') ? json({ error: { code: 'rights.missing' } }, 403) : json({ can_check: false, images: [] }),
    )
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ImageUpdatesTab nodeId="n1" />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/No image in use to follow yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull()
    expect(screen.queryByText('Private registries')).toBeNull()
  })
})

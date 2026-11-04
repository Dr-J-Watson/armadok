import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { PublicStatusPage } from '../features/status/PublicStatusPage'
import { PublishControl } from '../features/status/PublishControl'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('public status page', () => {
  it('shows the title, the state and the dots, and nothing else', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ title: 'Blog', state: 'down', since: null, checked_at: '2026-11-04T10:00:00+00:00', availability: 75, recent: [true, false] }),
    )
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter initialEntries={['/status/abc']}>
          <Routes>
            <Route path="/status/:slug" element={<PublicStatusPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('heading', { name: 'Blog' })).toBeInTheDocument()
    expect(screen.getByText('Down')).toBeInTheDocument()
    expect(screen.getByText('Available 75% of the checks.')).toBeInTheDocument()
  })

  it('says so when the address does not exist', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ error: { code: 'common.not_found' } }, 404))
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter initialEntries={['/status/nope']}>
          <Routes>
            <Route path="/status/:slug" element={<PublicStatusPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByText('This page does not exist or was closed.')).toBeInTheDocument()
  })
})

describe('publish control', () => {
  it('opens a page with the chosen title and shows the address', async () => {
    const sent: { method: string; body?: unknown }[] = []
    let published = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method === 'PUT') {
        published = true
        sent.push({ method, body: JSON.parse(String(init?.body)) })
        return json({ published: true, slug: 'SLUG', title: 'Blog' })
      }
      return json(published ? { published: true, slug: 'SLUG', title: 'Blog' } : { published: false, slug: null, title: null })
    })
    render(
      <QueryClientProvider client={client()}>
        <PublishControl nodeId="n1" probeId="p1" defaultTitle="Blog" />
      </QueryClientProvider>,
    )
    await userEvent.click(await screen.findByText('Public page'))
    await userEvent.click(screen.getByRole('button', { name: 'Open the page' }))
    expect(await screen.findByRole('link', { name: /\/status\/SLUG$/ })).toBeInTheDocument()
    expect(sent).toEqual([{ method: 'PUT', body: { title: 'Blog' } }])
  })
})

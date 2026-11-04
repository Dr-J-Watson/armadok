import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { StaticConfig } from '../features/traefik/StaticConfig'
import { lineDiff } from '../features/traefik/lineDiff'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('line diff', () => {
  it('marks what was added and what was removed, and keeps the rest', () => {
    expect(lineDiff('a\nb\nc', 'a\nx\nc')).toEqual([
      { kind: 'same', text: 'a' },
      { kind: 'del', text: 'b' },
      { kind: 'add', text: 'x' },
      { kind: 'same', text: 'c' },
    ])
    expect(lineDiff('same', 'same')?.every((l) => l.kind === 'same')).toBe(true)
  })

  it('gives up on a text too long to compare instead of freezing', () => {
    const long = Array.from({ length: 1600 }, (_, i) => String(i)).join('\n')
    expect(lineDiff(long, long + '\nx')).toBeNull()
  })
})

describe('static configuration', () => {
  it('explains how to set it up when the machine has no folder for it', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ configured: false }))
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <StaticConfig nodeId="n1" />
      </QueryClientProvider>,
    )
    expect(await screen.findByText(/no static configuration folder/)).toBeInTheDocument()
  })

  it('shows the changes first, saves naming the version it replaces, then offers the restart', async () => {
    const sent: { method: string; path: string; body?: Record<string, unknown> }[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input).replace('/api/v1', '')
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method === 'PUT') {
        sent.push({ method, path, body: JSON.parse(String(init?.body)) })
        return json({ sha256: 'b'.repeat(64), size: 10 })
      }
      if (path.endsWith('/history')) return json({ versions: [] })
      if (path.endsWith('/docker/containers')) return json({ containers: [{ id: 'abc123abc123', names: ['/proxy'], image: 'traefik:v3' }] })
      return json({ configured: true, name: 'traefik.yml', content: 'a: 1', sha256: 'a'.repeat(64) })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <StaticConfig nodeId="n1" />
      </QueryClientProvider>,
    )
    const box = await screen.findByLabelText('Contents of traefik.yml')
    await userEvent.clear(box)
    await userEvent.type(box, 'a: 2')
    await userEvent.click(screen.getByRole('button', { name: 'Review the changes' }))
    expect(screen.getByLabelText('Changes')).toHaveTextContent('- a: 1')
    expect(screen.getByLabelText('Changes')).toHaveTextContent('+ a: 2')
    expect(sent).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Save this version' }))
    await vi.waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0]).toMatchObject({ path: '/nodes/n1/traefik/static', body: { content: 'a: 2', expected_sha256: 'a'.repeat(64) } })
    expect(await screen.findByText(/Traefik keeps running with the old file/)).toBeInTheDocument()
  })
})

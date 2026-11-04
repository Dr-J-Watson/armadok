import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ResourcesTab } from '../features/docker/ResourcesTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const sent: { method: string; path: string; body?: Record<string, unknown> }[] = []

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('manual clean-up', () => {
  it('asks for a confirmation, then cleans one kind and shows the result', async () => {
    sent.length = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input).replace('/api/v1', '')
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method === 'POST') {
        sent.push({ method, path, body: JSON.parse(String(init?.body)) })
        return json({ removed: 2, space: 3145728 })
      }
      return json({ items: [], truncated: false })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ResourcesTab nodeId="n1" />
      </QueryClientProvider>,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Remove dangling images' }))
    expect(sent).toHaveLength(0) // nothing is removed before the confirmation
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clean up now' }))
    expect(await screen.findByText('2 removed, 3 MiB freed.')).toBeInTheDocument()
    expect(sent).toEqual([{ method: 'POST', path: '/nodes/n1/docker/prune', body: { kind: 'images' } }])
  })
})

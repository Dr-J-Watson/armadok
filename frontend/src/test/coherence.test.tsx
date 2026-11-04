import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { Coherence } from '../features/traefik/Coherence'

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

function show(answer: unknown, open = vi.fn()) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(answer))
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <Coherence nodeId="n1" onOpenFile={open} />
    </QueryClientProvider>,
  )
  return open
}

describe('configuration check', () => {
  it('lists problems in words and opens the file concerned', async () => {
    const open = show({
      issues: [
        { severity: 'error', code: 'router.service_missing', subject: 'blog@file', path: 'blog.yml', detail: 'gone@file' },
        { severity: 'info', code: 'middleware.unused', subject: 'spare@file', path: 'm.yml', detail: '' },
      ],
      errors: [],
      truncated: false,
    })
    expect(await screen.findByText(/its service gone@file does not exist/)).toBeInTheDocument()
    expect(screen.getByText(/1 error\(s\), 0 warning\(s\), 1 remark\(s\)/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'blog.yml' }))
    expect(open).toHaveBeenCalledWith('blog.yml')
  })

  it('says so when everything agrees', async () => {
    show({ issues: [], errors: [], truncated: false })
    expect(await screen.findByText('No problem found.')).toBeInTheDocument()
  })
})

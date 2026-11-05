import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { GitPanel } from '../features/git/GitPanel'
import { GitSource } from '../features/git/GitSource'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})
const wrap = (ui: React.ReactNode) =>
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>)

describe('git source', () => {
  it('clones a repository as a new project, guessing the name from the address', async () => {
    let sent: Record<string, unknown> = {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (String(input).endsWith('/git/clone')) {
        sent = JSON.parse(String(init?.body))
        return json({ project: 'app' }, 201)
      }
      return json({ error: { code: 'rights.missing' } }, 403)
    })
    const onCreated = vi.fn()
    wrap(<GitSource nodeId="n1" onDraft={vi.fn()} onCreated={onCreated} />)
    await userEvent.type(screen.getByLabelText('Repository address (https)'), 'https://git.example.com/org/App.git')
    expect(screen.getByLabelText('Project name')).toHaveValue('app')
    await userEvent.click(screen.getByRole('button', { name: 'Create the project from Git' }))
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith('app'))
    expect(sent).toEqual({ name: 'app', url: 'https://git.example.com/org/App.git', branch: 'main' })
  })

  it('says when the branch has moved on and updates then deploys on request', async () => {
    const calls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = String(input).replace('/api/v1', '')
      calls.push(`${init?.method ?? 'GET'} ${path}`)
      if (path === '/nodes/n1/git/projects/app' && (init?.method ?? 'GET') === 'GET')
        return json({
          source: { url: 'https://git.example.com/org/app', branch: 'main' },
          status: { commit: 'a'.repeat(40), subject: 'first', dirty: false, remote: 'b'.repeat(40) },
          error: null,
        })
      return json({}, 200)
    })
    wrap(<GitPanel nodeId="n1" project="app" />)
    expect(await screen.findByText('A newer version exists')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Update and deploy' }))
    await vi.waitFor(() => expect(calls).toContain('POST /nodes/n1/projects/app/actions'))
    expect(calls.indexOf('POST /nodes/n1/git/projects/app/update')).toBeLessThan(
      calls.indexOf('POST /nodes/n1/projects/app/actions'),
    )
  })

  it('shows nothing for a project that did not come from Git', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ source: null, status: null, error: null }))
    wrap(<GitPanel nodeId="n1" project="app" />)
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    expect(screen.queryByText('Git source')).toBeNull()
  })
})

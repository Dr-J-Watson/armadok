import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ProjectsTab } from '../features/projects/ProjectsTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
const SHA1 = 'a'.repeat(64)
const SHA2 = 'b'.repeat(64)
const puts: { path: string; body: { content: string; expected_sha256: string | null } }[] = []
let putStatus = 200

function server(opts: { projects?: unknown; fileSha?: () => string } = {}) {
  puts.length = 0
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (path === '/nodes/n1/projects')
      return json(opts.projects ?? { projects: [{ name: 'blog', files: ['compose.yaml'] }] })
    if (path === '/nodes/n1/projects/blog/files/compose.yaml') {
      if (method === 'PUT') {
        puts.push({ path, body: JSON.parse(String(init?.body)) })
        return putStatus === 200
          ? json({ sha256: SHA2, size: 5 })
          : json({ error: { code: 'worker.conflict' } }, 409)
      }
      return json({ content: 'services: {}\n', sha256: (opts.fileSha ?? (() => SHA1))(), size: 13 })
    }
    return json({ error: { code: 'common.not_found' } }, 404)
  })
}

function renderTab(url = '/nodes/n1?tab=projects') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[url]}>
        <ProjectsTab nodeId="n1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
  putStatus = 200
})

describe('projects tab', () => {
  it('lists projects and opens a file in the editor', async () => {
    server()
    renderTab()
    await userEvent.click(await screen.findByRole('button', { name: 'compose.yaml' }))
    expect(await screen.findByLabelText('Content of compose.yaml')).toHaveValue('services: {}\n')
  })

  it('saves naming the version it replaces, then shows the new state', async () => {
    server()
    renderTab('/nodes/n1?tab=projects&project=blog&file=compose.yaml')
    const editor = await screen.findByLabelText('Content of compose.yaml')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled() // nothing changed yet
    await userEvent.type(editor, '# note')
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
    expect(puts[0].body.expected_sha256).toBe(SHA1)
    expect(puts[0].body.content).toContain('# note')
  })

  it('on a conflict nothing is lost: the user chooses to reload or to keep their version', async () => {
    server({ fileSha: () => SHA2 })
    putStatus = 409
    renderTab('/nodes/n1?tab=projects&project=blog&file=compose.yaml')
    await userEvent.type(await screen.findByLabelText('Content of compose.yaml'), '# mine')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/changed on the machine since you opened it/)).toBeInTheDocument()
    expect(screen.getByLabelText('Content of compose.yaml')).toHaveValue('services: {}\n# mine')
    // Keep mine: the next save names the newer version it was shown, and sends my text.
    putStatus = 200
    await userEvent.click(screen.getByRole('button', { name: /Keep my version/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
    expect(puts[1].body.expected_sha256).toBe(SHA2)
    expect(puts[1].body.content).toContain('# mine')
  })

  it('explains an unconfigured machine instead of showing a bare error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ error: { code: 'worker.projects_not_configured' } }, 409),
    )
    renderTab()
    expect(await screen.findByRole('alert')).toHaveTextContent('No projects folder is configured')
  })

  it('shows an empty state when the folder has no Compose project', async () => {
    server({ projects: { projects: [] } })
    renderTab()
    expect(await screen.findByText(/No Compose project found/)).toBeInTheDocument()
  })
})

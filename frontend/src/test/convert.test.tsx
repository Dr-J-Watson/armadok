import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { NewProject } from '../features/projects/NewProject'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

let created: Record<string, unknown> | null = null

function server(extra: (path: string, init?: RequestInit) => Response | null = () => null) {
  created = null
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const mine = extra(path, init)
    if (mine) return mine
    if (path === '/modules') return json({ modules: ['convert', 'templates', 'projects'] })
    if (path === '/nodes/n1/projects' && init?.method === 'POST') {
      created = JSON.parse(String(init.body))
      return json({ project: 'blog', file: 'compose.yaml', env: true }, 201)
    }
    if (path === '/templates') return json([])
    return json({ error: { code: 'common.not_found' } }, 404)
  })
}

const wrap = (onCreated = vi.fn()) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <NewProject nodeId="n1" onCreated={onCreated} onCancel={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  )

describe('new project from a converted command', () => {
  it('fills the draft from a docker run command, keeps secrets in the .env and creates the project', async () => {
    server((path) =>
      path === '/convert/docker-run'
        ? json({
            compose: 'services:\n  blog:\n    image: nginx\n',
            env: 'DB_PASSWORD=x\n',
            service: 'blog',
            warnings: [{ code: 'convert.unsupported_flag', detail: '--weird' }],
          })
        : null,
    )
    const onCreated = vi.fn()
    wrap(onCreated)
    await userEvent.click(await screen.findByLabelText('A docker run command'))
    await userEvent.type(screen.getByLabelText('Your docker run command'), 'docker run -d nginx')
    await userEvent.click(screen.getByRole('button', { name: 'Convert' }))
    expect(await screen.findByText('The option --weird is not converted. Check whether you need it.')).toBeInTheDocument()
    expect(screen.getByLabelText('Project name')).toHaveValue('blog')
    expect(screen.getByLabelText('.env')).toHaveValue('DB_PASSWORD=x\n')
    await userEvent.click(screen.getByRole('button', { name: 'Create the project' }))
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith('blog'))
    expect(created).toEqual({ name: 'blog', compose: 'services:\n  blog:\n    image: nginx\n', env: 'DB_PASSWORD=x\n' })
  })

  it('says why a command could not be read, with the server code translated', async () => {
    server((path) => (path === '/convert/docker-run' ? json({ error: { code: 'convert.not_run' } }, 422) : null))
    wrap()
    await userEvent.click(await screen.findByLabelText('A docker run command'))
    await userEvent.type(screen.getByLabelText('Your docker run command'), 'docker ps')
    await userEvent.click(screen.getByRole('button', { name: 'Convert' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Only docker run commands can be converted.')
  })

  it('converts the ticked running containers into the draft', async () => {
    let sent: Record<string, unknown> = {}
    server((path, init) => {
      if (path === '/nodes/n1/docker/containers')
        return json({ containers: [{ id: 'a'.repeat(64), names: ['blog'], image: 'nginx', state: 'running' }] })
      if (path === '/nodes/n1/convert/containers') {
        sent = JSON.parse(String(init?.body))
        return json({ compose: 'services:\n  blog:\n    image: nginx\n', env: null, service: 'blog', warnings: [] })
      }
      return null
    })
    wrap()
    await userEvent.click(await screen.findByLabelText('Containers already running here'))
    const go = await screen.findByRole('button', { name: 'Convert 0 containers' })
    expect(go).toBeDisabled()
    await userEvent.click(screen.getByLabelText(/blog/))
    await userEvent.click(screen.getByRole('button', { name: 'Convert 1 container' }))
    expect(await screen.findByLabelText('compose.yaml')).toHaveValue('services:\n  blog:\n    image: nginx\n')
    expect(sent).toEqual({ ids: ['a'.repeat(64)] })
  })

  it('offers to keep the draft as a template', async () => {
    let kept: Record<string, unknown> = {}
    server((path, init) => {
      if (path === '/templates' && init?.method === 'POST') {
        kept = JSON.parse(String(init.body))
        return json({ id: 't1', name: 'x', description: '' }, 201)
      }
      return null
    })
    wrap()
    await userEvent.type(await screen.findByLabelText('Project name'), 'shop')
    await userEvent.click(screen.getByRole('button', { name: 'Keep as a template' }))
    expect(await screen.findByText(/Template kept/)).toBeInTheDocument()
    expect(kept.name).toBe('shop')
    expect(kept).not.toHaveProperty('env')
  })
})

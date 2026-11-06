import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { IntegrationsPage } from '../features/integrations/IntegrationsPage'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
// Assembled, so no secret-looking literal sits in the source.
const SHOWN = ['shown', 'once', 'value'].join('-')
const calls: { method: string; path: string; body: unknown }[] = []
let hooks: unknown[] = []

function server() {
  calls.length = 0
  hooks = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const path = String(input).replace('/api/v1', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method !== 'GET') calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : null })
    if (path === '/hooks' && method === 'GET') return json({ hooks, actions: ['project.redeploy'] })
    if (path === '/hooks' && method === 'POST') {
      hooks = [{ id: 'abcd', name: 'CI', action: 'project.redeploy', target: { node_id: 'n1', project: 'blog' }, expires_at: null, revoked: false, last_used_at: null, last_result: null }]
      return json({ id: 'abcd', token: SHOWN, fire_path: '/api/v1/hooks/abcd/fire' }, 201)
    }
    if (path === '/nodes') return json([{ id: 'n1', name: 'Home', type: 'full' }])
    if (path === '/nodes/n1/projects') return json({ projects: [{ name: 'blog', files: ['compose.yaml'] }] })
    return json({}, 204)
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('integrations', () => {
  it('creates a hook, shows its token once with a ready curl line, and can revoke it', async () => {
    server()
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <IntegrationsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await userEvent.type(await screen.findByLabelText('Name'), 'CI')
    await userEvent.selectOptions(screen.getByLabelText('Node'), 'n1')
    await userEvent.selectOptions(await screen.findByLabelText('Project'), 'blog')
    await userEvent.click(screen.getByRole('button', { name: 'Create the hook' }))
    expect(await screen.findByDisplayValue(SHOWN)).toBeInTheDocument()
    expect(screen.getByText(/curl -X POST -H "Authorization: Bearer shown-once-value"/)).toBeInTheDocument()
    expect(calls[0]).toMatchObject({ path: '/hooks', body: { name: 'CI', action: 'project.redeploy', target: { node_id: 'n1', project: 'blog' } } })
    await userEvent.click(screen.getByRole('button', { name: 'I have copied it' }))
    expect(screen.queryByDisplayValue(SHOWN)).not.toBeInTheDocument()
    expect(await screen.findByText('Home / blog', { exact: false })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(calls.at(-1)).toMatchObject({ method: 'DELETE', path: '/hooks/abcd' })
  })
})

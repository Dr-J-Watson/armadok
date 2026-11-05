import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { ConvertPage } from '../features/convert/ConvertPage'
import { FromContainers } from '../features/convert/FromContainers'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

const wrap = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ConvertPage />
    </QueryClientProvider>,
  )

describe('converter', () => {
  it('sends the pasted command and shows the file, the secrets apart, and what to check', async () => {
    let sent: Record<string, unknown> = {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_input, init) => {
      sent = JSON.parse(String(init?.body))
      return json({
        compose: 'services:\n  blog:\n    image: nginx\n',
        env: 'DB_PASSWORD=x\n',
        service: 'blog',
        warnings: [{ code: 'convert.unsupported_flag', detail: '--weird' }],
      })
    })
    wrap()
    await userEvent.type(screen.getByLabelText('Your docker run command'), 'docker run -d nginx')
    await userEvent.click(screen.getByRole('button', { name: 'Convert' }))
    expect(await screen.findByText(/image: nginx/)).toBeInTheDocument()
    expect(sent).toEqual({ command: 'docker run -d nginx' })
    expect(screen.getByText('The option --weird is not converted. Check whether you need it.')).toBeInTheDocument()
    expect(screen.getByText('DB_PASSWORD=x')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Copy' })).toHaveLength(2)
  })

  it('says why a command could not be read, with the server code translated', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ error: { code: 'convert.not_run' } }, 422))
    wrap()
    await userEvent.type(screen.getByLabelText('Your docker run command'), 'docker ps')
    await userEvent.click(screen.getByRole('button', { name: 'Convert' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Only docker run commands can be converted.')
  })

  it('converts the ticked containers of a machine', async () => {
    let sent: Record<string, unknown> = {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if (String(input).endsWith('/docker/containers')) {
        return json({ containers: [{ id: 'a'.repeat(64), names: ['blog'], image: 'nginx', state: 'running' }] })
      }
      sent = JSON.parse(String(init?.body))
      return json({ compose: 'services:\n  blog:\n    image: nginx\n', env: null, service: 'blog', warnings: [] })
    })
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <FromContainers nodeId="n1" />
      </QueryClientProvider>,
    )
    const go = await screen.findByRole('button', { name: 'Convert 0 containers' })
    expect(go).toBeDisabled()
    await userEvent.click(screen.getByLabelText(/blog/))
    await userEvent.click(screen.getByRole('button', { name: 'Convert 1 container' }))
    expect(await screen.findByText(/image: nginx/)).toBeInTheDocument()
    expect(sent).toEqual({ ids: ['a'.repeat(64)] })
  })
})

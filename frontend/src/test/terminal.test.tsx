import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { TerminalPanel } from '../features/terminal/TerminalPanel'

vi.mock('../core/auth', () => ({
  useAuth: () => ({ state: { status: 'authenticated', user: { totp_enabled: true } } }),
}))
// xterm needs a real canvas; what matters here is the panel around it.
vi.mock('../features/terminal/TerminalView', () => ({
  TerminalView: ({ ticket }: { ticket: string }) => <div>terminal {ticket}</div>,
}))

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})
const wrap = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <TerminalPanel nodeId="n1" project="app" />
    </QueryClientProvider>,
  )
const settings = (over: object) => ({ enabled: false, writable: false, network: false, image: 'alpine:3.20', can_manage: false, ...over })

describe('project terminal', () => {
  it('shows nothing when it is off and the person cannot turn it on', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(settings({})))
    wrap()
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalled())
    expect(screen.queryByText('Terminal')).toBeNull()
  })

  it('opens with the fresh authenticator code and hands the ticket to the terminal', async () => {
    let sent: Record<string, unknown> = {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_i, init) => {
      if (init?.method === 'POST') {
        sent = JSON.parse(String(init.body))
        return json({ ticket: 'tkt', writable: false }, 201)
      }
      return json(settings({ enabled: true }))
    })
    wrap()
    await userEvent.type(await screen.findByLabelText('Code from your authenticator app'), '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Open a terminal' }))
    expect(await screen.findByText('terminal tkt')).toBeInTheDocument()
    expect(sent).toEqual({ code: '123456' })
  })

  it('lets someone allowed change the settings, with the code', async () => {
    let sent: Record<string, unknown> = {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_i, init) => {
      if (init?.method === 'PUT') {
        sent = JSON.parse(String(init.body))
        return new Response(null, { status: 204 })
      }
      return json(settings({ can_manage: true }))
    })
    wrap()
    await userEvent.click(await screen.findByText('Terminal settings'))
    await userEvent.click(screen.getByLabelText('Allow a terminal on this project'))
    await userEvent.type(screen.getAllByLabelText('Code from your authenticator app')[0], '654321')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await vi.waitFor(() => expect(sent).toMatchObject({ enabled: true, writable: false, network: false, code: '654321' }))
  })
})

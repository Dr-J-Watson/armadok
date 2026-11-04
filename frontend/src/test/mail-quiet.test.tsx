import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { Channels } from '../features/notifications/Channels'
import { Quiet } from '../features/notifications/Quiet'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

function renderWith(ui: React.ReactElement, answers: Record<string, unknown>) {
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    const key = `${init?.method ?? 'GET'} ${String(url).replace('/api/v1', '')}`
    return json(answers[key] ?? {})
  })
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>)
  return spy
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('e-mail channel', () => {
  it('sends the SMTP settings, with the port as a number', async () => {
    const spy = renderWith(<Channels />, { 'GET /notifications/channels': [], 'POST /notifications/channels': { id: 'c1', secret: null } })
    await userEvent.selectOptions(await screen.findByLabelText('Kind'), 'email')
    await userEvent.type(screen.getByLabelText('Name'), 'Mail')
    await userEvent.type(screen.getByLabelText('SMTP server'), 'smtp.example.com')
    await userEvent.type(screen.getByLabelText('Sent from'), 'armadok@example.com')
    await userEvent.type(screen.getByLabelText('Sent to'), 'me@example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Add the channel' }))
    const post = spy.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(JSON.parse(String(post![1]!.body))).toMatchObject({
      kind: 'email',
      settings: { host: 'smtp.example.com', port: 587, security: 'starttls', sender: 'armadok@example.com', recipient: 'me@example.com' },
    })
  })

  it('asks for the code before an unconfirmed mailbox is used', async () => {
    const spy = renderWith(<Channels />, {
      'GET /notifications/channels': [{ id: 'c1', kind: 'email', name: 'Mail', last_status: null, confirmed: false }],
      'POST /notifications/channels/c1/confirm': { confirmed: true },
    })
    expect(await screen.findByText(/A code was sent to this address/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Send a test' })).toBeNull()
    await userEvent.type(screen.getByLabelText('Confirmation code'), '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    const post = spy.mock.calls.find(([url]) => String(url).endsWith('/c1/confirm'))
    expect(JSON.parse(String(post![1]!.body))).toEqual({ code: '123456' })
  })

  it('warns that no credentials are accepted without encryption', async () => {
    renderWith(<Channels />, { 'GET /notifications/channels': [] })
    await userEvent.selectOptions(await screen.findByLabelText('Kind'), 'email')
    await userEvent.selectOptions(screen.getByLabelText('Encryption'), 'none')
    expect(screen.getByText(/no user name or password is accepted/)).toBeInTheDocument()
  })
})

describe('quiet hours', () => {
  it('shows the stored hours and saves a change', async () => {
    const spy = renderWith(<Quiet />, {
      'GET /notifications/quiet': { enabled: false, start: '22:00', end: '07:00', timezone: 'Europe/Paris' },
      'PUT /notifications/quiet': { enabled: true, start: '22:00', end: '07:00', timezone: 'Europe/Paris' },
    })
    await userEvent.click(await screen.findByLabelText('Turn on quiet hours'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    const put = spy.mock.calls.find(([, init]) => init?.method === 'PUT')
    expect(JSON.parse(String(put![1]!.body))).toEqual({ enabled: true, start: '22:00', end: '07:00', timezone: 'Europe/Paris' })
    expect(await screen.findByText(/Saved/)).toBeInTheDocument()
  })
})

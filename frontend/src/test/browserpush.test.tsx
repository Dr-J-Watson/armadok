import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { BrowserPush, keyToBytes } from '../features/notifications/BrowserPush'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  setAccessToken(null)
})

const wrap = (done = () => undefined) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <BrowserPush onDone={done} />
    </QueryClientProvider>,
  )

describe('browser push', () => {
  it('turns the server key from base64url into bytes', () => {
    expect(Array.from(keyToBytes('AQID'))).toEqual([1, 2, 3])
    expect(Array.from(keyToBytes('_-8'))).toEqual([255, 239])
  })

  it('subscribes this browser and hands the keys to the server as a channel', async () => {
    const sent: { path: string; body: Record<string, unknown> }[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      if ((init?.method ?? 'GET') === 'POST') {
        sent.push({ path: String(input), body: JSON.parse(String(init?.body)) })
        return json({ id: 'c1', secret: null })
      }
      return json({ public_key: 'AQID' })
    })
    const subscribe = vi.fn().mockResolvedValue({
      toJSON: () => ({ endpoint: 'https://push.example.com/abc', keys: { p256dh: 'P', auth: 'A' } }),
    })
    vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('granted') })
    vi.stubGlobal('PushManager', function PushManager() {})
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: { getRegistration: vi.fn().mockResolvedValue({ pushManager: { subscribe } }) },
    })
    const done = vi.fn()
    wrap(done)
    expect(screen.getByText(/sees that a message exists, never what it says/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Turn on in this browser' }))
    await vi.waitFor(() => expect(done).toHaveBeenCalled())
    expect(subscribe).toHaveBeenCalledWith(expect.objectContaining({ userVisibleOnly: true }))
    expect(sent[0]).toEqual({
      path: '/api/v1/notifications/channels',
      body: { kind: 'webpush', name: 'This browser', settings: { endpoint: 'https://push.example.com/abc', p256dh: 'P', auth: 'A' } },
    })
  })

  it('says so, and subscribes nothing, when the permission is refused', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    vi.stubGlobal('Notification', { requestPermission: vi.fn().mockResolvedValue('denied') })
    vi.stubGlobal('PushManager', function PushManager() {})
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { getRegistration: vi.fn() } })
    wrap()
    await userEvent.click(screen.getByRole('button', { name: 'Turn on in this browser' }))
    expect(await screen.findByText(/refused the permission/)).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

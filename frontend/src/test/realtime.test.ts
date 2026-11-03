import { QueryClient } from '@tanstack/react-query'
import { setAccessToken } from '../core/api'
import { applySignal, startRealtime } from '../core/realtime'
import type { Feature } from '../core/registry'

class FakeSocket {
  static all: FakeSocket[] = []
  sent: string[] = []
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  constructor(public url: string) {
    FakeSocket.all.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {
    this.closed = true
  }
  say(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }
  end(code: number) {
    this.onclose?.({ code } as CloseEvent)
  }
}

function client() {
  const qc = new QueryClient()
  qc.setQueryData(['containers', 'n1'], 1)
  qc.setQueryData(['containers', 'n2'], 1)
  qc.setQueryData(['users'], 1)
  qc.setQueryData(['roles'], 1)
  return qc
}
const stale = (qc: QueryClient, key: unknown[]) => qc.getQueryState(key)?.isInvalidated === true
const features: Feature[] = [{ name: 'roles', signals: { 'roles.changed': ['roles'] } }]

beforeEach(() => {
  FakeSocket.all = []
  vi.useFakeTimers()
  setAccessToken('tok')
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('applySignal', () => {
  it('reloads only what the signal concerns', () => {
    const qc = client()
    applySignal(qc, features, { signal: 'docker.changed', node: 'n1' })
    expect(stale(qc, ['containers', 'n1'])).toBe(true)
    expect(stale(qc, ['containers', 'n2'])).toBe(false)
    expect(stale(qc, ['users'])).toBe(false)
  })

  it('uses the feature that declared the signal for changes that belong to no machine', () => {
    const qc = client()
    applySignal(qc, features, { signal: 'roles.changed', node: null })
    expect(stale(qc, ['roles'])).toBe(true)
    expect(stale(qc, ['users'])).toBe(false)
  })

  it('reloads everything when the server says we fell behind', () => {
    const qc = client()
    applySignal(qc, features, { signal: '*', node: null })
    expect(['users', 'roles'].every((k) => stale(qc, [k]))).toBe(true)
  })
})

describe('connection', () => {
  const socket = () => (url: string) => new FakeSocket(url) as unknown as WebSocket

  it('sends the token in the first message, never in the address', () => {
    const states: string[] = []
    startRealtime({ queryClient: client(), features, onStatus: (s) => states.push(s), socket: socket() })
    const ws = FakeSocket.all[0]
    expect(ws.url).toMatch(/^wss?:\/\/[^?]+\/api\/v1\/realtime$/)
    ws.onopen?.()
    expect(JSON.parse(ws.sent[0])).toEqual({ token: 'tok' })
    ws.say({ signal: 'ready', node: null })
    expect(states).toEqual(['connecting', 'live'])
  })

  it('ignores messages that are not signals', () => {
    const qc = client()
    startRealtime({ queryClient: qc, features, onStatus: () => {}, socket: socket() })
    const ws = FakeSocket.all[0]
    ws.onmessage?.({ data: 'not json' } as MessageEvent)
    ws.say({ nothing: 1 })
    ws.say({ signal: 5 })
    expect(stale(qc, ['users'])).toBe(false)
  })

  it('reconnects with growing pauses and starts over after a good connection', async () => {
    const states: string[] = []
    startRealtime({ queryClient: client(), features, onStatus: (s) => states.push(s), socket: socket() })
    FakeSocket.all[0].end(1006)
    await vi.advanceTimersByTimeAsync(999)
    expect(FakeSocket.all).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(FakeSocket.all).toHaveLength(2)
    FakeSocket.all[1].end(1006)
    await vi.advanceTimersByTimeAsync(1999)
    expect(FakeSocket.all).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(FakeSocket.all).toHaveLength(3)
    FakeSocket.all[2].say({ signal: 'ready', node: null })
    FakeSocket.all[2].end(1006)
    await vi.advanceTimersByTimeAsync(1000)
    expect(FakeSocket.all).toHaveLength(4)
    expect(states.at(-1)).toBe('connecting')
  })

  it('stops for good when closed by the page', async () => {
    const stop = startRealtime({ queryClient: client(), features, onStatus: () => {}, socket: socket() })
    stop()
    expect(FakeSocket.all[0].closed).toBe(true)
    FakeSocket.all[0].end(1006)
    await vi.advanceTimersByTimeAsync(60000)
    expect(FakeSocket.all).toHaveLength(1)
  })
})

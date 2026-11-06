import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, renderHook, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import '../core/i18n'
import { setAccessToken } from '../core/api'
import { useModules } from '../core/modules'
import { RealtimeProvider } from '../core/RealtimeProvider'

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('stable screens', () => {
  it('hands out the same set of modules on every render', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ modules: ['users', 'nodes'] }))
    const { result, rerender } = renderHook(() => useModules(), { wrapper: wrapper() })
    await vi.waitFor(() => expect(result.current).toBeDefined())
    const first = result.current
    rerender()
    rerender()
    // A new object per render would restart the live connection and reload every screen.
    expect(result.current).toBe(first)
  })

  it('opens the live connection once, however often the screen draws again', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json({ modules: ['users', 'nodes'] }))
    const opened: string[] = []
    class FakeSocket {
      onopen: (() => void) | null = null
      onmessage: ((e: MessageEvent) => void) | null = null
      onclose: ((e: CloseEvent) => void) | null = null
      constructor(url: string) {
        opened.push(url)
        queueMicrotask(() => this.onopen?.())
      }
      send() {
        queueMicrotask(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ signal: 'ready', node: null }) })))
      }
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeSocket)
    const Wrapper = wrapper()
    const view = render(
      <Wrapper>
        <RealtimeProvider>
          <p>inside</p>
        </RealtimeProvider>
      </Wrapper>,
    )
    await screen.findByText('inside')
    await vi.waitFor(() => expect(opened.length).toBeGreaterThanOrEqual(1))
    await new Promise((r) => setTimeout(r, 200))
    view.rerender(
      <Wrapper>
        <RealtimeProvider>
          <p>inside</p>
        </RealtimeProvider>
      </Wrapper>,
    )
    await new Promise((r) => setTimeout(r, 200))
    expect(opened).toHaveLength(1)
    vi.unstubAllGlobals()
  })
})

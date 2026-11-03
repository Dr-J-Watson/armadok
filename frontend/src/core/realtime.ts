// The live-update connection: one WebSocket that says "something changed", never what.
//
// A signal only names an area (`docker.changed`) and maybe a machine. The screen answers by
// reloading the affected queries through the API, which applies the rights. The access token
// travels in the first message (a WebSocket cannot carry a header, and a token in the address
// would end up in logs) and stays in memory like everywhere else.

import type { QueryClient } from '@tanstack/react-query'
import { currentAccessToken, refreshSession } from './api'
import type { Feature } from './registry'

export type RealtimeStatus = 'connecting' | 'live' | 'offline'

const MIN_DELAY_MS = 1000
const MAX_DELAY_MS = 30000

interface Options {
  queryClient: QueryClient
  features: Feature[]
  onStatus: (status: RealtimeStatus) => void
  // Replaceable for tests.
  socket?: (url: string) => WebSocket
}

// Turn a signal into reloads. Exported for tests.
export function applySignal(
  queryClient: QueryClient,
  features: Feature[],
  message: { signal: string; node: string | null },
): void {
  // "Too far behind": anything may have changed.
  if (message.signal === '*') {
    void queryClient.invalidateQueries()
    return
  }
  if (message.node) {
    const node = message.node
    void queryClient.invalidateQueries({ predicate: (q) => q.queryKey.includes(node) })
  }
  for (const feature of features) {
    for (const key of feature.signals?.[message.signal] ?? []) {
      void queryClient.invalidateQueries({ queryKey: [key] })
    }
  }
}

// Open the connection and keep it open (with growing pauses between attempts) until closed.
export function startRealtime(options: Options): () => void {
  let closed = false
  let ws: WebSocket | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let delay = MIN_DELAY_MS

  const open = options.socket ?? ((url: string) => new WebSocket(url))

  function connect() {
    if (closed) return
    options.onStatus('connecting')
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = open(`${scheme}//${location.host}/api/v1/realtime`)
    ws = socket
    socket.onopen = () => socket.send(JSON.stringify({ token: currentAccessToken() }))
    socket.onmessage = (event: MessageEvent) => {
      let message: { signal?: unknown; node?: unknown }
      try {
        message = JSON.parse(String(event.data))
      } catch {
        return
      }
      if (typeof message.signal !== 'string') return
      if (message.signal === 'ready') {
        delay = MIN_DELAY_MS
        options.onStatus('live')
        // Anything that changed while we were away.
        void options.queryClient.invalidateQueries()
        return
      }
      applySignal(options.queryClient, options.features, {
        signal: message.signal,
        node: typeof message.node === 'string' ? message.node : null,
      })
    }
    socket.onclose = (event: CloseEvent) => {
      options.onStatus('offline')
      if (closed) return
      // 4401: the token is old. Get a new one first; if that fails the login page takes over.
      const again = event.code === 4401 ? refreshSession() : Promise.resolve(true)
      void again.then((ok) => {
        if (closed || !ok) return
        timer = setTimeout(connect, delay)
        delay = Math.min(delay * 2, MAX_DELAY_MS)
      })
    }
  }

  connect()
  return () => {
    closed = true
    clearTimeout(timer)
    ws?.close()
  }
}

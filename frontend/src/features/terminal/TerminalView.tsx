import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { currentAccessToken } from '../../core/api'

// Bytes <-> base64 without going through text, so any output of the shell survives.
const toBase64 = (bytes: Uint8Array) => btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(''))
const fromBase64 = (text: string) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0))

// The terminal of a project. The one-time ticket was obtained by opening it; the access token
// goes in the first message (never in the address). Typed keys and output only pass through the
// browser's memory: nothing is stored.
export function TerminalView({ ticket, onClose }: { ticket: string; onClose: () => void }) {
  const { t } = useTranslation('terminal')
  const host = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<'connecting' | 'open' | 'ended'>('connecting')

  useEffect(() => {
    if (!host.current) return
    const term = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000 })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host.current)
    fit.fit()
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${scheme}//${location.host}/api/v1/terminal/${encodeURIComponent(ticket)}`)
    ws.onopen = () => {
      ws.send(JSON.stringify({ token: currentAccessToken() }))
      ws.send(JSON.stringify({ resize: [term.cols, term.rows] }))
      setState('open')
    }
    ws.onmessage = (event: MessageEvent) => {
      let message: { out?: unknown; closed?: unknown }
      try {
        message = JSON.parse(String(event.data))
      } catch {
        return
      }
      if (typeof message.out === 'string') term.write(fromBase64(message.out))
      if (message.closed === true) setState('ended')
    }
    ws.onclose = () => setState('ended')
    const typing = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ in: toBase64(new TextEncoder().encode(data)) }))
    })
    const sizing = term.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ resize: [cols, rows] }))
    })
    const onWindow = () => fit.fit()
    window.addEventListener('resize', onWindow)
    return () => {
      window.removeEventListener('resize', onWindow)
      typing.dispose()
      sizing.dispose()
      ws.close()
      term.dispose()
    }
  }, [ticket])

  return (
    <section aria-label={t('terminal:title')} className="terminal-box">
      <div className="actions between">
        <span className={`badge ${state === 'open' ? 'ok' : 'off'}`}>{t(`terminal:state.${state}`)}</span>
        <button type="button" onClick={onClose}>
          {t('terminal:close')}
        </button>
      </div>
      <div ref={host} className="terminal-host" />
    </section>
  )
}

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'

type Ask = (message: string, options?: { danger?: boolean }) => Promise<boolean>

const ConfirmContext = createContext<Ask | null>(null)

interface Pending {
  message: string
  danger: boolean
  done: (answer: boolean) => void
}

// A confirmation inside the page, in the look of the interface, instead of the browser's own
// box. `const confirm = useConfirm()` then `if (await confirm('...')) ...`. Escape or a click
// outside cancels; the focus starts on the safe answer.
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation()
  const [pending, setPending] = useState<Pending | null>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const ask = useCallback<Ask>(
    (message, options) => new Promise((resolve) => setPending({ message, danger: options?.danger ?? true, done: resolve })),
    [],
  )
  const answer = (value: boolean) => {
    pending?.done(value)
    setPending(null)
  }
  useEffect(() => {
    if (!pending) return
    cancel.current?.focus()
    const escape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        pending.done(false)
        setPending(null)
      }
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [pending])
  return (
    <ConfirmContext.Provider value={ask}>
      {children}
      {pending && (
        <div className="modal-backdrop" onClick={() => answer(false)}>
          <div role="alertdialog" aria-modal="true" aria-label={pending.message} className="modal" onClick={(e) => e.stopPropagation()}>
            <p>{pending.message}</p>
            <div className="actions">
              <button type="button" className={pending.danger ? 'danger-button' : 'primary'} onClick={() => answer(true)}>
                {t('confirm.yes')}
              </button>
              <button type="button" ref={cancel} onClick={() => answer(false)}>
                {t('confirm.no')}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): Ask {
  const ask = useContext(ConfirmContext)
  // Without a provider (a test of one component) fall back to the browser's own box.
  return ask ?? (async (message) => window.confirm(message))
}

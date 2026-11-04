import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { useCanFn } from '../../core/auth'
import { useFeatures } from '../../core/useFeatures'

interface Hit {
  type: string
  label: string
  detail: string
  to: string
}

const DEBOUNCE_MS = 250

// Ctrl+K (or ⌘K): jump to a page, or to a machine, person, note or domain by typing a few
// letters. Pages come from the features' own navigation; the rest from the server's search,
// which only returns what the account may see.
export function CommandPalette() {
  const { t } = useTranslation(['palette', 'common'])
  const navigate = useNavigate()
  const can = useCanFn()
  const features = useFeatures()
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [asked, setAsked] = useState('')
  const [active, setActive] = useState(0)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setOpen((value) => !value)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const remote = useQuery({
    queryKey: ['palette', asked],
    enabled: open && asked.length >= 2,
    // A POST: what is typed may be a piece of a private note, and must stay out of URLs and logs.
    queryFn: () => api.post<{ results: Hit[] }>('/search', { q: asked }),
    staleTime: 10_000,
    retry: false,
  })

  const needle = text.trim().toLowerCase()
  const pages: Hit[] = features
    .flatMap((f) => f.nav ?? [])
    .filter((item) => !item.requiresRight || can(item.requiresRight))
    .map((item) => ({ type: 'page', label: t(item.labelKey, { ns: 'common' }), detail: '', to: item.to }))
    .filter((hit) => needle === '' || hit.label.toLowerCase().includes(needle))
  const found = needle.length >= 2 ? (remote.data?.results ?? []) : []
  const items = [...pages, ...found]
  const current = Math.min(active, Math.max(items.length - 1, 0))

  function close() {
    setOpen(false)
    setText('')
    setAsked('')
    setActive(0)
  }
  function onChange(value: string) {
    setText(value)
    setActive(0)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setAsked(value.trim().toLowerCase()), DEBOUNCE_MS)
  }
  function go(hit: Hit) {
    close()
    // Only a path inside the application: whatever the server sent, never another site.
    if (hit.to.startsWith('/') && !hit.to.startsWith('//')) navigate(hit.to)
  }
  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === 'Escape') close()
    else if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive(Math.min(current + 1, items.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive(Math.max(current - 1, 0))
    } else if (event.key === 'Enter' && items[current]) {
      event.preventDefault()
      go(items[current])
    }
  }

  return (
    <>
      <button type="button" onClick={() => setOpen(true)} aria-label={t('palette:open')} title={t('palette:open')}>
        {t('palette:button')}
      </button>
      {open && (
        <div className="palette-backdrop" onClick={close}>
          <div role="dialog" aria-modal="true" aria-label={t('palette:title')} className="palette" onClick={(e) => e.stopPropagation()} onKeyDown={onKeyDown}>
            <input
              ref={input}
              autoFocus
              role="combobox"
              aria-expanded="true"
              aria-controls="palette-list"
              aria-activedescendant={items[current] ? `palette-${current}` : undefined}
              placeholder={t('palette:placeholder')}
              value={text}
              maxLength={64}
              onChange={(e) => onChange(e.target.value)}
            />
            <ul id="palette-list" role="listbox">
              {items.map((hit, index) => (
                <li
                  key={`${index}:${hit.type}:${hit.to}:${hit.label}`}
                  id={`palette-${index}`}
                  role="option"
                  aria-selected={index === current}
                  className={index === current ? 'active' : ''}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => go(hit)}
                >
                  <span className="badge off">{t(`palette:type.${hit.type}`, { defaultValue: hit.type })}</span> {hit.label}
                  {hit.detail && <small> — {hit.detail}</small>}
                </li>
              ))}
              {items.length === 0 && <li className="empty">{remote.isFetching ? t('palette:searching') : t('palette:none')}</li>}
            </ul>
          </div>
        </div>
      )}
    </>
  )
}

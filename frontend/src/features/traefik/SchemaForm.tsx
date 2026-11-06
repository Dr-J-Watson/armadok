// The form of one middleware, drawn from the catalogue of its type. It edits a plain object, so
// anything the catalogue does not know about stays in it untouched.

import { ArrowDown, ArrowUp, KeyRound, X } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Tip } from '../../components/Tip'
import type { Field } from './middlewareSchema'

type Obj = Record<string, unknown>

export interface Names {
  middlewares: string[]
  services: string[]
}

interface Props {
  fields: Field[]
  value: Obj
  onChange: (next: Obj) => void
  names: Names
  // Turns a user name and password into an htpasswd entry (asked of the server).
  onHash?: (username: string, password: string) => Promise<string>
}

const lines = (v: unknown): string => (Array.isArray(v) ? v.join('\n') : '')
const toList = (text: string): string[] => text.split('\n').map((x) => x.trim()).filter(Boolean)
const kvText = (v: unknown): string => (v && typeof v === 'object' ? Object.entries(v as Obj).map(([k, x]) => `${k}: ${String(x)}`).join('\n') : '')
const toKv = (text: string): Obj => Object.fromEntries(toList(text).map((l) => { const i = l.indexOf(':'); return i < 0 ? [l, ''] : [l.slice(0, i).trim(), l.slice(i + 1).trim()] }))

export function SchemaForm({ fields, value, onChange, names, onHash }: Props) {
  const { t } = useTranslation(['traefik'])
  const set = (k: string, v: unknown) => onChange({ ...value, [k]: v })
  return (
    <div className="schema-form">
      {fields.map((field) => {
        const label = (
          <span>
            {field.k}
            {field.required && <span aria-hidden="true"> *</span>}
            {field.help && <Tip text={t(`traefik:schema.help.${field.k}`)} />}
          </span>
        )
        const v = value[field.k]
        if (field.t === 'o' && field.flat) {
          return (
            <fieldset key={field.k}>
              <legend>{t(`traefik:schema.group.${field.k}`, { defaultValue: field.k })}</legend>
              <SchemaForm fields={field.fields ?? []} value={value} onChange={onChange} names={names} />
            </fieldset>
          )
        }
        if (field.t === 'o')
          return (
            <fieldset key={field.k}>
              <legend>{field.k}</legend>
              <SchemaForm fields={field.fields ?? []} value={(v as Obj) ?? {}} onChange={(next) => set(field.k, next)} names={names} />
            </fieldset>
          )
        if (field.t === 'b')
          return (
            <label key={field.k} className="check">
              <input type="checkbox" checked={v === true} onChange={(e) => set(field.k, e.target.checked ? true : undefined)} /> {field.k}
            </label>
          )
        if (field.t === 'n')
          return (
            <label key={field.k} className="field">
              {label}
              <input type="number" value={typeof v === 'number' ? v : ''} placeholder={field.ph} onChange={(e) => set(field.k, e.target.value === '' ? undefined : Number(e.target.value))} />
            </label>
          )
        if (field.t === 'e')
          return (
            <label key={field.k} className="field">
              {label}
              <select value={typeof v === 'string' ? v : ''} onChange={(e) => set(field.k, e.target.value || undefined)}>
                <option value="" />
                {(field.opts ?? []).map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
            </label>
          )
        if (field.t === 'sl')
          return (
            <div key={field.k} className="field">
              <label>
                {label}
                <textarea rows={Math.min(8, Math.max(2, Array.isArray(v) ? v.length + 1 : 2))} spellCheck={false} placeholder={field.ph} value={lines(v)} onChange={(e) => set(field.k, toList(e.target.value))} />
              </label>
              {field.hash && onHash && <Hasher onHash={onHash} onEntry={(entry) => set(field.k, [...(Array.isArray(v) ? v : []), entry])} />}
            </div>
          )
        if (field.t === 'kv')
          return (
            <label key={field.k} className="field">
              {label}
              <textarea rows={3} spellCheck={false} placeholder="X-Header: value" value={kvText(v)} onChange={(e) => set(field.k, toKv(e.target.value))} />
            </label>
          )
        if (field.t === 'ref') {
          const options = field.ref === 'services' ? names.services : names.middlewares
          return (
            <label key={field.k} className="field">
              {label}
              <input list={`names-${field.k}`} value={typeof v === 'string' ? v : ''} onChange={(e) => set(field.k, e.target.value)} />
              <datalist id={`names-${field.k}`}>{options.map((x) => <option key={x} value={x} />)}</datalist>
            </label>
          )
        }
        if (field.t === 'refl') return <RefList key={field.k} label={label} items={Array.isArray(v) ? (v as string[]) : []} options={names.middlewares} onChange={(items) => set(field.k, items)} />
        return (
          <label key={field.k} className="field">
            {label}
            <input value={typeof v === 'string' ? v : ''} placeholder={field.ph} spellCheck={false} onChange={(e) => set(field.k, e.target.value)} />
          </label>
        )
      })}
    </div>
  )
}

// An ordered list of names of other items (the middlewares of a chain).
function RefList({ label, items, options, onChange }: { label: React.ReactNode; items: string[]; options: string[]; onChange: (items: string[]) => void }) {
  const { t } = useTranslation(['traefik'])
  const [adding, setAdding] = useState('')
  const move = (i: number, by: number) => {
    const next = [...items]
    ;[next[i], next[i + by]] = [next[i + by], next[i]]
    onChange(next)
  }
  return (
    <div className="field">
      {label}
      <ol className="ref-list">
        {items.map((x, i) => (
          <li key={`${x}-${i}`}>
            <span className="badge">{i + 1}</span>
            <span className="grow">{x}</span>
            <button type="button" className="icon-button" disabled={i === 0} aria-label={t('traefik:schema.up')} onClick={() => move(i, -1)}><ArrowUp size={14} aria-hidden="true" /></button>
            <button type="button" className="icon-button" disabled={i === items.length - 1} aria-label={t('traefik:schema.down')} onClick={() => move(i, 1)}><ArrowDown size={14} aria-hidden="true" /></button>
            <button type="button" className="icon-button" aria-label={t('traefik:schema.remove')} onClick={() => onChange(items.filter((_, j) => j !== i))}><X size={14} aria-hidden="true" /></button>
          </li>
        ))}
      </ol>
      <div className="actions">
        <input list="chain-options" value={adding} placeholder={t('traefik:schema.addRef')} onChange={(e) => setAdding(e.target.value)} />
        <datalist id="chain-options">{options.map((x) => <option key={x} value={x} />)}</datalist>
        <button type="button" disabled={adding.trim() === ''} onClick={() => { onChange([...items, adding.trim()]); setAdding('') }}>{t('traefik:schema.add')}</button>
      </div>
    </div>
  )
}

// A user name and password become an htpasswd entry on the server; the password goes nowhere else.
function Hasher({ onHash, onEntry }: { onHash: (u: string, p: string) => Promise<string>; onEntry: (entry: string) => void }) {
  const { t } = useTranslation(['traefik'])
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  return (
    <div className="actions">
      <input aria-label={t('traefik:schema.user')} placeholder={t('traefik:schema.user')} value={user} autoComplete="off" onChange={(e) => setUser(e.target.value)} />
      <input aria-label={t('traefik:schema.password')} placeholder={t('traefik:schema.password')} type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} />
      <button
        type="button"
        disabled={busy || user.trim() === '' || password.length < 8}
        onClick={async () => {
          setBusy(true)
          setError(false)
          try {
            onEntry(await onHash(user.trim(), password))
            setUser('')
            setPassword('')
          } catch {
            setError(true)
          } finally {
            setBusy(false)
          }
        }}
      >
        <KeyRound size={14} aria-hidden="true" /> {t('traefik:schema.hash')}
      </button>
      {error && <span role="alert" className="error">{t('traefik:schema.hashFailed')}</span>}
    </div>
  )
}

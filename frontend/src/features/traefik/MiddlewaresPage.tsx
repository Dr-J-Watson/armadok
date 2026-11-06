// Every middleware of the node, read from all its configuration files and sorted by what it
// does. The ones this form made can be edited, renamed, copied and deleted here; the ones written
// by hand open their file. Nothing is changed by reading.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Box, Copy, CornerUpRight, Database, FileKey, FileType, FolderMinus, FolderPlus, Gauge, KeyRound, Link2, Network,
  Package, Pencil, Plug, Plus, Puzzle, Replace, RotateCw, Search, Shield, ShieldCheck, Trash2, TriangleAlert, Wrench, Zap,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Problem } from '../../components/Problem'
import { Tip } from '../../components/Tip'
import { useConfirm } from '../../components/Confirm'
import { api } from '../../core/api'
import { MiddlewareEditor } from './MiddlewareEditor'
import { INTENTS, blank, fromSpec } from './middlewareForm'
import type { Intent, Item } from './middlewareForm'
import { GROUPS, INTENT_TYPE, kindOf } from './middlewareKinds'
import type { MiddlewareGroup } from './middlewareKinds'

interface FileMiddleware {
  name: string
  id: string
  type: string
  path: string
  refs: string[]
}
interface FileRouter {
  name: string
  middlewares: string[]
}
interface Sites {
  routers: FileRouter[]
  middlewares: FileMiddleware[]
  errors: { path: string; error: string }[]
}

const ICONS = { Box, CornerUpRight, Database, FileKey, FileType, FolderMinus, FolderPlus, Gauge, KeyRound, Link2, Network, Package, Plug, Puzzle, Replace, RotateCw, Shield, ShieldCheck, TriangleAlert, Zap }
export const Icon = ({ name, size = 18 }: { name: string; size?: number }) => {
  const Component = ICONS[name as keyof typeof ICONS] ?? Box
  return <Component size={size} aria-hidden="true" />
}

type Mode = { kind: 'gallery' } | { kind: 'edit'; item: Item | null; initial: ReturnType<typeof blank> } | null

export function MiddlewaresPage({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['traefik', 'errors'])
  const confirm = useConfirm()
  const client = useQueryClient()
  const base = `/nodes/${nodeId}/traefik/middlewares`
  const [filter, setFilter] = useState('')
  const [mode, setMode] = useState<Mode>(null)
  const sites = useQuery({ queryKey: ['traefik-sites', nodeId], queryFn: () => api.get<Sites>(`/nodes/${nodeId}/traefik/sites`) })
  const managed = useQuery({ queryKey: ['traefik-middlewares', nodeId], queryFn: () => api.get<{ middlewares: Item[] }>(base), retry: false })
  const mine = useMemo(() => new Map((managed.data?.middlewares ?? []).map((m) => [m.name, m])), [managed.data])

  // Who uses each middleware: the routers that list it and the chains that contain it.
  const usedBy = useMemo(() => {
    const result = new Map<string, string[]>()
    const add = (id: string, who: string) => result.set(id, [...new Set([...(result.get(id) ?? []), who])])
    for (const r of sites.data?.routers ?? []) for (const m of r.middlewares) add(m, r.name)
    for (const m of sites.data?.middlewares ?? []) for (const ref of m.refs) add(ref, m.name)
    return result
  }, [sites.data])

  const all = sites.data?.middlewares ?? []
  const needle = filter.trim().toLowerCase()
  const shown = all
    .filter((m) => !needle || `${m.name} ${m.type} ${kindOf(m.type).label} ${t(`traefik:mw.type.${m.type}`, { defaultValue: '' })}`.toLowerCase().includes(needle))
    .sort((a, b) => a.name.localeCompare(b.name))
  const sections = GROUPS.map((group) => ({ group, rows: shown.filter((m) => kindOf(m.type).group === group) })).filter((s) => s.rows.length > 0)

  const remove = useMutation({
    mutationFn: (m: Item) => api.del(`${base}/${m.name}?expected_sha256=${m.sha256}`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['traefik-middlewares', nodeId] })
      await client.invalidateQueries({ queryKey: ['traefik-sites', nodeId] })
    },
  })

  // Renaming follows the sites the form built; the plan is shown first, and hand-written files
  // that name the middleware are listed because nothing rewrites them.
  const [renaming, setRenaming] = useState<{ item: Item; name: string } | null>(null)
  const plan = useMutation({
    mutationFn: () => api.post<{ sites: string[]; manual: string[] }>(`${base}/${renaming!.item.name}/rename`, { new_name: renaming!.name, expected_sha256: renaming!.item.sha256, dry_run: true }),
  })
  const rename = useMutation({
    mutationFn: () => api.post(`${base}/${renaming!.item.name}/rename`, { new_name: renaming!.name, expected_sha256: renaming!.item.sha256 }),
    onSuccess: async () => {
      setRenaming(null)
      plan.reset()
      await Promise.all(['traefik-middlewares', 'traefik-sites', 'traefik-managed'].map((k) => client.invalidateQueries({ queryKey: [k, nodeId] })))
    },
  })
  const failed = sites.error ?? remove.error ?? plan.error ?? rename.error

  if (mode?.kind === 'gallery')
    return <Gallery onPick={(intent) => setMode({ kind: 'edit', item: null, initial: blank(intent) })} onCancel={() => setMode(null)} />
  if (mode?.kind === 'edit')
    return <MiddlewareEditor nodeId={nodeId} editing={mode.item} initial={mode.initial} onDone={() => setMode(null)} onCancel={() => setMode(null)} />


  return (
    <>
      <header className="page-head page-head-split">
        <div>
          <h1>
            {t('traefik:mw.title')}
            <Tip text={t('traefik:mw.tip')} />
          </h1>
          <p className="hint">{t('traefik:mw.count', { count: all.length })} · {t('traefik:mw.subtitle')}</p>
        </div>
        <button type="button" className="primary" onClick={() => setMode({ kind: 'gallery' })}>
          <Plus size={16} aria-hidden="true" /> {t('traefik:middlewares.new')}
        </button>
      </header>
      {failed != null && <Problem error={failed} />}
      {(sites.data?.errors ?? []).map((e) => (
        <p key={e.path} className="notice" role="alert">
          {t('traefik:badFile', { path: e.path })}
        </p>
      ))}
      <label className="search-box">
        <Search size={15} aria-hidden="true" />
        <span className="visually-hidden">{t('traefik:mw.search')}</span>
        <input type="search" placeholder={t('traefik:mw.search')} value={filter} onChange={(e) => setFilter(e.target.value)} />
      </label>
      {sites.isPending && <p className="loading">{t('loading')}</p>}
      {sites.isSuccess && sections.length === 0 && (
        <div className="empty">
          <p>{all.length === 0 ? t('traefik:middlewares.none') : t('traefik:mw.noMatch')}</p>
          {all.length === 0 && (
            <button type="button" className="primary" onClick={() => setMode({ kind: 'gallery' })}>
              <Plus size={14} aria-hidden="true" /> {t('traefik:mw.createFirst')}
            </button>
          )}
        </div>
      )}
      {renaming && (
        <div className="notice" role="group" aria-label={t('traefik:middlewares.renameTitle')}>
          <label>
            {t('traefik:middlewares.newName')}
            <input value={renaming.name} maxLength={64} onChange={(e) => { setRenaming({ ...renaming, name: e.target.value }); plan.reset() }} />
          </label>{' '}
          <button type="button" disabled={plan.isPending || renaming.name === renaming.item.name || renaming.name === ''} onClick={() => plan.mutate()}>
            {t('traefik:middlewares.renamePlan')}
          </button>{' '}
          <button type="button" onClick={() => { setRenaming(null); plan.reset() }}>{t('traefik:middlewares.renameCancel')}</button>
          {plan.data && (
            <>
              <p>{t('traefik:middlewares.renameSites', { count: plan.data.sites.length, sites: plan.data.sites.join(', ') })}</p>
              {plan.data.manual.length > 0 && (
                <p role="alert" className="error">{t('traefik:middlewares.renameManual', { files: plan.data.manual.join(', ') })}</p>
              )}
              <button type="button" className="primary" disabled={rename.isPending} onClick={() => rename.mutate()}>{t('traefik:middlewares.renameGo')}</button>
            </>
          )}
        </div>
      )}
      {sections.map(({ group, rows }) => (
        <section key={group} className="mw-section" aria-label={t(`traefik:mw.groups.${group}`)}>
          <div className="mw-cat">
            <h2>{t(`traefik:mw.groups.${group}`)}</h2>
            <span className="badge">{rows.length}</span>
            <Tip text={t(`traefik:mw.groupTips.${group}`)} />
          </div>
          <div className="mw-grid">
            {rows.map((m) => {
              const mine1 = mine.get(m.name)
              const users = (usedBy.get(m.id) ?? []).sort()
              const kind = kindOf(m.type)
              return (
                <article key={`${m.path}:${m.id}`} className="mw-card" data-group={group as MiddlewareGroup}>
                  <header>
                    <span className="mw-icon" aria-hidden="true"><Icon name={kind.icon} size={16} /></span>
                    <div className="grow">
                      <strong>{m.name}</strong>
                      <span className="hint">
                        <b>{kind.label}</b> · {t(`traefik:mw.type.${m.type}`, { defaultValue: m.type })}
                      </span>
                    </div>
                  </header>
                  <div className="used" title={t('traefik:mw.usedTip')}>
                    <span className="hint">{t('traefik:mw.usedBy')}</span>
                    {users.length === 0 ? <span className="badge warn">{t('traefik:mw.nobody')}</span> : (
                      <>
                        {users.slice(0, 4).map((u) => <span key={u} className="badge accent">{u}</span>)}
                        {users.length > 4 && <span className="badge">+{users.length - 4}</span>}
                      </>
                    )}
                  </div>
                  <footer>
                    <span className="hint ellipsis" title={m.path}>{m.path}</span>
                    {mine1 ? (
                      <span className="mw-actions">
                        <button type="button" className="icon-button" title={t('traefik:mw.copy')} aria-label={t('traefik:mw.copy')} onClick={() => setMode({ kind: 'edit', item: null, initial: { ...fromSpec(mine1.spec), name: `${mine1.name}-copy`.slice(0, 40) } })}>
                          <Copy size={14} aria-hidden="true" />
                        </button>
                        <button type="button" className="icon-button" title={t('traefik:middlewares.edit')} aria-label={`${t('traefik:middlewares.edit')} ${m.name}`} onClick={() => setMode({ kind: 'edit', item: mine1, initial: fromSpec(mine1.spec) })}>
                          <Pencil size={14} aria-hidden="true" />
                        </button>
                        <button type="button" className="icon-button" title={t('traefik:middlewares.rename')} aria-label={`${t('traefik:middlewares.rename')} ${m.name}`} onClick={() => { setRenaming({ item: mine1, name: mine1.name }); plan.reset() }}>
                          <Wrench size={14} aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          className="icon-button danger"
                          disabled={users.length > 0}
                          title={users.length > 0 ? t('traefik:middlewares.inUse') : t('traefik:middlewares.delete')}
                          aria-label={`${t('traefik:middlewares.delete')} ${m.name}`}
                          onClick={async () => (await confirm(t('traefik:middlewares.confirmDelete'))) && remove.mutate(mine1)}
                        >
                          <Trash2 size={14} aria-hidden="true" />
                        </button>
                      </span>
                    ) : (
                      <Link className="button-link small" to={`/nodes/${nodeId}?tab=traefik-advanced&view=files&file=${encodeURIComponent(m.path)}`} title={t('traefik:mw.byHandTip')}>
                        <Pencil size={13} aria-hidden="true" /> {t('traefik:mw.openFile')}
                      </Link>
                    )}
                  </footer>
                </article>
              )
            })}
          </div>
        </section>
      ))}
    </>
  )
}

// First step of a new middleware: say what you want to do, not which Traefik type it is.
function Gallery({ onPick, onCancel }: { onPick: (intent: Intent) => void; onCancel: () => void }) {
  const { t } = useTranslation(['traefik'])
  return (
    <section aria-label={t('traefik:mw.galleryTitle')}>
      <h2>{t('traefik:mw.galleryTitle')}</h2>
      <p className="hint">{t('traefik:mw.galleryHint')}</p>
      <div className="mw-gallery">
        {INTENTS.map((intent) => {
          const kind = kindOf(INTENT_TYPE[intent])
          return (
            <button key={intent} type="button" className="mw-choice" data-group={kind.group} onClick={() => onPick(intent)}>
              <span className="mw-icon" aria-hidden="true"><Icon name={kind.icon} /></span>
              <span>
                <strong>{t(`traefik:intent.${intent}`)}</strong>
                <span className="hint">{t(`traefik:mw.intentDesc.${intent}`)}</span>
                <code>{kind.label}</code>
              </span>
            </button>
          )
        })}
      </div>
      <div className="actions">
        <button type="button" onClick={onCancel}>{t('traefik:middlewares.cancel')}</button>
      </div>
    </section>
  )
}

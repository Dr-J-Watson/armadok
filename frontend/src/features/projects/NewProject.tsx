import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import type { ProjectDraft } from '../../core/registry'
import { useFeatures } from '../../core/useFeatures'

const EMPTY = 'services:\n  app:\n    image: nginx:1.27\n    ports:\n      - "8080:80"\n'
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/

// Create a project on a machine: pick where it starts from (nothing, a template, a docker run
// command, running containers... whatever the enabled features offer), check the Compose text
// and the private .env, give it a name. Nothing runs: deploying is a separate step.
export function NewProject({ nodeId, onCreated, onCancel }: { nodeId: string; onCreated: (name: string) => void; onCancel: () => void }) {
  const { t } = useTranslation(['projects', 'errors'])
  const client = useQueryClient()
  const features = useFeatures()
  const sources = features.flatMap((f) => f.projectSources ?? [])
  const actions = features.flatMap((f) => f.projectDraftActions ?? [])
  const [source, setSource] = useState<string>('empty')
  const [draft, setDraft] = useState<ProjectDraft | null>(null)
  const [name, setName] = useState('')
  const [compose, setCompose] = useState(EMPTY)
  const [env, setEnv] = useState('')

  const take = (d: ProjectDraft) => {
    setDraft(d)
    setCompose(d.compose)
    setEnv(d.env ?? '')
    if (d.name && !name) setName(d.name.toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^[^a-z0-9]+/, '').slice(0, 64))
  }
  const create = useMutation({
    mutationFn: () => api.post(`/nodes/${nodeId}/projects`, { name, compose, env: env.trim() ? env : null }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['projects', nodeId] })
      onCreated(name)
    },
  })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    create.mutate()
  }
  const current = sources.find((s) => s.id === source)
  const ready = draft !== null || source === 'empty'
  return (
    <section className="panel" aria-label={t('projects:new.title')}>
      <h2>{t('projects:new.title')}</h2>
      <fieldset>
        <legend>{t('projects:new.from')}</legend>
        <div className="segmented">
          {[{ id: 'empty', label: t('projects:new.empty') }, ...sources.map((s) => ({ id: s.id, label: t(s.labelKey) }))].map((s) => (
            <label key={s.id} className="legend-item">
              <input
                type="radio"
                name="source"
                checked={source === s.id}
                onChange={() => {
                  setSource(s.id)
                  setDraft(null)
                  if (s.id === 'empty') {
                    setCompose(EMPTY)
                    setEnv('')
                  }
                }}
              />{' '}
              {s.label}
            </label>
          ))}
        </div>
      </fieldset>
      {current && <div>{current.render({ nodeId, onDraft: take, onCreated })}</div>}
      {ready && (
        <form onSubmit={submit}>
          {draft?.notes}
          <label>
            {t('projects:new.name')}
            <input value={name} onChange={(e) => setName(e.target.value)} pattern={NAME.source} maxLength={64} required />
          </label>
          <label>
            compose.yaml
            <textarea className="editor" rows={14} spellCheck={false} value={compose} onChange={(e) => setCompose(e.target.value)} required />
          </label>
          <label>
            .env
            <textarea className="editor" rows={4} spellCheck={false} value={env} onChange={(e) => setEnv(e.target.value)} />
          </label>
          <p className="hint">{t('projects:new.envHint')}</p>
          {create.isError && <Problem error={create.error} />}
          <div className="actions">
            <button type="submit" className="primary" disabled={create.isPending || !NAME.test(name) || !compose.trim()}>
              {t('projects:new.create')}
            </button>
            {actions.map((a) => (
              <span key={a.id}>{a.render({ draft: { name, compose, env } })}</span>
            ))}
            <button type="button" onClick={onCancel}>
              {t('projects:new.cancel')}
            </button>
          </div>
        </form>
      )}
    </section>
  )
}

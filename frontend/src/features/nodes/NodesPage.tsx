import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { ChevronRight, Plus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { NodeIcon } from '../../components/NodeIcon'
import type { Node, NodeType } from './types'
import { Problem } from '../../components/Problem'

const TYPES: NodeType[] = ['full', 'docker', 'network']

export function NodesPage() {
  const { t } = useTranslation(['nodes', 'errors'])
  const queryClient = useQueryClient()
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [type, setType] = useState<NodeType>('full')
  const [description, setDescription] = useState('')

  const create = useMutation({
    mutationFn: () => api.post<Node>('/nodes', { name, type, description }),
    onSuccess: () => {
      setName('')
      setDescription('')
      setAdding(false)
      return queryClient.invalidateQueries({ queryKey: ['nodes'] })
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  return (
    <>
      <header className="page-head page-head-split">
        <div>
          <h1>{t('nodes:title')}</h1>
          <p className="hint">{t('nodes:subtitle')}</p>
        </div>
        <button type="button" className="primary" onClick={() => setAdding(!adding)} aria-expanded={adding}>
          <Plus size={16} aria-hidden="true" /> {t('nodes:create.open')}
        </button>
      </header>

      {adding && (
        <form onSubmit={submit} className="panel form-grid" aria-label={t('nodes:create.title')}>
          <label htmlFor="node-name">
            {t('nodes:create.name')}
            <input id="node-name" required autoFocus maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label htmlFor="node-type">
            {t('nodes:create.type')}
            <select id="node-type" value={type} onChange={(e) => setType(e.target.value as NodeType)}>
              {TYPES.map((value) => (
                <option key={value} value={value}>
                  {t(`nodes:types.${value}`)}
                </option>
              ))}
            </select>
            <span className="hint">{t(`nodes:typeHints.${type}`)}</span>
          </label>
          <label htmlFor="node-description" className="wide">
            {t('nodes:create.description')}
            <input id="node-description" maxLength={300} value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          {create.isError && (
            <p role="alert" className="error wide">
              {errorMessage(t, create.error)}
            </p>
          )}
          <div className="wide actions">
            <button type="submit" className="primary" disabled={create.isPending}>
              {t('nodes:create.submit')}
            </button>
            <button type="button" onClick={() => setAdding(false)}>
              {t('nodes:create.cancel')}
            </button>
          </div>
        </form>
      )}

      {nodes.isPending && <p className="loading">{t('loading')}</p>}
      {nodes.isError && (
        <Problem error={nodes.error} />
      )}
      {nodes.data?.length === 0 && !adding && (
        <div className="empty">
          <p>{t('nodes:empty')}</p>
          <button type="button" className="primary" onClick={() => setAdding(true)}>
            <Plus size={16} aria-hidden="true" /> {t('nodes:create.open')}
          </button>
        </div>
      )}
      <ul className="cards node-cards">
        {nodes.data?.map((node) => (
          <li key={node.id}>
            <Link to={`/nodes/${node.id}`} className="node-card">
              <span className="node-icon" aria-hidden="true">
                <NodeIcon type={node.type} />
              </span>
              <span className="node-card-body">
                <strong>{node.name}</strong>
                <span className="node-meta">
                  <span className="badge">{t(`nodes:types.${node.type}`)}</span>
                  {node.local && <span className="badge ok">{t('nodes:local')}</span>}
                  {node.suspended && (
                    <span className="badge off" title={node.suspended_reason ?? undefined}>
                      {t('nodes:suspended')}
                    </span>
                  )}
                </span>
                {(node.local || node.description) && (
                  <span className="hint">{node.local ? t('nodes:localDescription') : node.description}</span>
                )}
              </span>
              <ChevronRight size={18} aria-hidden="true" className="chevron" />
            </Link>
          </li>
        ))}
      </ul>
    </>
  )
}

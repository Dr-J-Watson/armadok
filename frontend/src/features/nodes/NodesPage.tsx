import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import type { Node, NodeType } from './types'

const TYPES: NodeType[] = ['full', 'docker', 'network']

export function NodesPage() {
  const { t } = useTranslation(['nodes', 'errors'])
  const queryClient = useQueryClient()
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const [name, setName] = useState('')
  const [type, setType] = useState<NodeType>('full')
  const [description, setDescription] = useState('')

  const create = useMutation({
    mutationFn: () => api.post<Node>('/nodes', { name, type, description }),
    onSuccess: () => {
      setName('')
      setDescription('')
      return queryClient.invalidateQueries({ queryKey: ['nodes'] })
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  return (
    <>
      <h1>{t('nodes:title')}</h1>

      {nodes.isPending && <p>{t('loading')}</p>}
      {nodes.isError && (
        <p role="alert" className="error">
          {errorMessage(t, nodes.error)}
        </p>
      )}
      {nodes.data?.length === 0 && <p className="empty">{t('nodes:empty')}</p>}
      <ul className="cards">
        {nodes.data?.map((node) => (
          <li key={node.id}>
            <Link to={`/nodes/${node.id}`}>
              <strong>{node.name}</strong>
              <span className="badge">{t(`nodes:types.${node.type}`)}</span>
            </Link>
            {node.description && <p>{node.description}</p>}
          </li>
        ))}
      </ul>

      <section>
        <h2>{t('nodes:create.title')}</h2>
        <form onSubmit={submit} className="form">
          <label htmlFor="node-name">{t('nodes:create.name')}</label>
          <input
            id="node-name"
            required
            maxLength={100}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <label htmlFor="node-type">{t('nodes:create.type')}</label>
          <select id="node-type" value={type} onChange={(e) => setType(e.target.value as NodeType)}>
            {TYPES.map((value) => (
              <option key={value} value={value}>
                {t(`nodes:types.${value}`)}
              </option>
            ))}
          </select>
          <p className="hint">{t(`nodes:typeHints.${type}`)}</p>
          <label htmlFor="node-description">{t('nodes:create.description')}</label>
          <input
            id="node-description"
            maxLength={1000}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          {create.isError && (
            <p role="alert" className="error">
              {errorMessage(t, create.error)}
            </p>
          )}
          <button type="submit" className="primary" disabled={create.isPending}>
            {t('nodes:create.submit')}
          </button>
        </form>
      </section>
    </>
  )
}

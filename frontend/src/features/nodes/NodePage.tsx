import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { useCanFn } from '../../core/auth'
import { errorMessage } from '../../core/errors'
import { useModules } from '../../core/modules'
import type { NodeTab } from '../../core/registry'
import { features } from '../enabled'
import { useFeatures } from '../../core/useFeatures'
import type { Node } from './types'

export function NodePage() {
  const { t } = useTranslation(['nodes', 'errors'])
  const { nodeId = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const modules = useModules()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const sections = useFeatures().flatMap((f) => f.objectSections ?? [])

  const node = useQuery({
    queryKey: ['nodes', nodeId],
    queryFn: () => api.get<Node>(`/nodes/${nodeId}`),
  })

  const remove = useMutation({
    mutationFn: () => api.del(`/nodes/${nodeId}`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['nodes'] })
      navigate('/nodes')
    },
  })

  // Tabs are contributed by features; only those whose module is enabled and that apply to
  // this node's type are shown.
  const allTabs = useMemo<NodeTab[]>(() => {
    if (!node.data || !modules) return []
    return features
      .filter((f) => !f.requiresModule || modules.has(f.requiresModule))
      .flatMap((f) => f.nodeTabs ?? [])
      .filter((tab) => !tab.nodeTypes || tab.nodeTypes.includes(node.data.type))
  }, [node.data, modules])

  const can = useCanFn()
  const tabs = allTabs.filter((tab) => !tab.requiresRight || can(tab.requiresRight))

  if (node.isPending) return <p>{t('loading')}</p>
  if (node.isError) {
    return (
      <>
        <p role="alert" className="error">
          {errorMessage(t, node.error)}
        </p>
        <Link to="/nodes">{t('nodes:back')}</Link>
      </>
    )
  }

  const current = tabs.find((tab) => tab.id === search.get('tab'))

  return (
    <>
      <p>
        <Link to="/nodes">{t('nodes:back')}</Link>
      </p>
      <h1>
        {node.data.name} <span className="badge">{t(`nodes:types.${node.data.type}`)}</span>
        {node.data.local && <span className="badge ok">{t('nodes:local')}</span>}
      </h1>
      {node.data.description && <p>{node.data.description}</p>}

      <div role="tablist" className="tabs">
        <button
          type="button"
          role="tab"
          aria-selected={!current}
          onClick={() => setSearch({}, { replace: true })}
        >
          {t('nodes:tabs.overview')}
        </button>
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={current?.id === tab.id}
            onClick={() => setSearch({ tab: tab.id }, { replace: true })}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>

      <section role="tabpanel">
        {current ? (
          current.render({ nodeId, nodeType: node.data.type })
        ) : (
          <dl className="details">
            <dt>{t('nodes:details.created')}</dt>
            <dd>{new Date(node.data.created_at).toLocaleString()}</dd>
            <dt>{t('nodes:details.id')}</dt>
            <dd>
              <code>{node.data.id}</code>
            </dd>
          </dl>
        )}
        {!current &&
          sections.map((section) => (
            <div key={section.id}>
              {section.render({ nodeId, objectType: 'node', objectId: nodeId, objectLabel: node.data.name })}
            </div>
          ))}
      </section>

      {!node.data.local && (
      <section className="danger">
        <h2>{t('nodes:delete.title')}</h2>
        <p>{t('nodes:delete.explain')}</p>
        {remove.isError && (
          <p role="alert" className="error">
            {errorMessage(t, remove.error)}
          </p>
        )}
        {confirmDelete ? (
          <>
            <button type="button" className="danger-button" onClick={() => remove.mutate()}>
              {t('nodes:delete.confirm')}
            </button>{' '}
            <button type="button" onClick={() => setConfirmDelete(false)}>
              {t('nodes:delete.cancel')}
            </button>
          </>
        ) : (
          <button type="button" onClick={() => setConfirmDelete(true)}>
            {t('nodes:delete.button')}
          </button>
        )}
      </section>
      )}
    </>
  )
}

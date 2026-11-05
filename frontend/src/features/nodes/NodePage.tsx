import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { ChevronLeft } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../../core/api'
import { useAuth, useCanFn } from '../../core/auth'
import { useModules } from '../../core/modules'
import type { NodeTab } from '../../core/registry'
import { features } from '../enabled'
import { useFeatures } from '../../core/useFeatures'
import { NodeIcon } from '../../components/NodeIcon'
import type { Node } from './types'
import { Problem } from '../../components/Problem'

export function NodePage() {
  const { t } = useTranslation(['nodes', 'errors'])
  const { nodeId = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const modules = useModules()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [code, setCode] = useState('')
  const auth = useAuth()
  // With a second factor on the account, deleting a machine asks for a fresh code.
  const needsCode = auth.state.status === 'authenticated' && auth.state.user.totp_enabled
  const sections = useFeatures().flatMap((f) => f.objectSections ?? [])

  const node = useQuery({
    queryKey: ['nodes', nodeId],
    queryFn: () => api.get<Node>(`/nodes/${nodeId}`),
  })

  const remove = useMutation({
    mutationFn: () => api.del(`/nodes/${nodeId}`, needsCode ? { code } : undefined),
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
        <Problem error={node.error} />
        <Link to="/nodes">{t('nodes:back')}</Link>
      </>
    )
  }

  const current = tabs.find((tab) => tab.id === search.get('tab'))

  return (
    <>
      <nav className="crumbs" aria-label={t('nodes:back')}>
        <Link to="/nodes">
          <ChevronLeft size={16} aria-hidden="true" /> {t('nodes:back')}
        </Link>
      </nav>
      <header className="page-head">
        <span className="node-icon" aria-hidden="true">
          <NodeIcon type={node.data.type} size={22} />
        </span>
        <div>
          <h1>
            {node.data.name}
            <span className="badge">{t(`nodes:types.${node.data.type}`)}</span>
            {node.data.local && <span className="badge ok">{t('nodes:local')}</span>}
          </h1>
          {(node.data.local || node.data.description) && (
            <p className="hint">{node.data.local ? t('nodes:localDescription') : node.data.description}</p>
          )}
        </div>
      </header>

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
          <div className="overview">
            <div className="panel">
              <h2>{t('nodes:details.title')}</h2>
              <dl className="details">
                <dt>{t('nodes:details.type')}</dt>
                <dd>{t(`nodes:types.${node.data.type}`)}</dd>
                <dt>{t('nodes:details.created')}</dt>
                <dd>{new Date(node.data.created_at).toLocaleString()}</dd>
                <dt>{t('nodes:details.id')}</dt>
                <dd>
                  <code>{node.data.id}</code>
                </dd>
              </dl>
            </div>
            {sections.map((section) => (
              <div key={section.id} className="panel">
                {section.render({ nodeId, objectType: 'node', objectId: nodeId, objectLabel: node.data.name })}
              </div>
            ))}
            {!node.data.local && (
              <details className="danger-zone">
                <summary>{t('nodes:delete.zone')}</summary>
                <h3>{t('nodes:delete.title')}</h3>
                <p>{t('nodes:delete.explain')}</p>
                {remove.isError && (
                  <Problem error={remove.error} />
                )}
                {confirmDelete ? (
                  <>
                    {needsCode && (
                      <label>
                        {t('nodes:delete.code')}
                        <input
                          value={code}
                          onChange={(e) => setCode(e.target.value)}
                          inputMode="numeric"
                          autoComplete="one-time-code"
                        />
                      </label>
                    )}{' '}
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
              </details>
            )}
          </div>
        )}
      </section>
    </>
  )
}

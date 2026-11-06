import { useConfirm } from '../../components/Confirm'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import type { ProjectSourceProps } from '../../core/registry'

interface Template {
  id: string
  name: string
  description: string
  mine: boolean
  owner_name: string | null
  shared_all: boolean
  groups: string[]
}

interface Group {
  id: string
  name: string
}

// The person's own templates: pick one to start a project from it, or delete it.
export function TemplateSource({ onDraft }: ProjectSourceProps) {
  const { t } = useTranslation(['templates', 'errors'])
  const client = useQueryClient()
  const ask = useConfirm()
  const list = useQuery({ queryKey: ['templates'], queryFn: () => api.get<Template[]>('/templates') })
  const use = useMutation({
    mutationFn: (id: string) => api.get<{ name: string; compose: string }>(`/templates/${id}`),
    onSuccess: (tpl) => onDraft({ name: tpl.name, compose: tpl.compose }),
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/templates/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: ['templates'] }),
  })
  const groups = useQuery({ queryKey: ['template-groups'], queryFn: () => api.get<Group[]>('/templates/groups') })
  const share = useMutation({
    mutationFn: ({ id, everyone, groups: g }: { id: string; everyone: boolean; groups: string[] }) =>
      api.put(`/templates/${id}/sharing`, { everyone, groups: g }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['templates'] }),
  })
  if (list.isError) return <Problem error={list.error} />
  if (list.data?.length === 0) return <p className="empty">{t('templates:none')}</p>
  return (
    <>
      <ul className="links">
        {(list.data ?? []).map((tpl) => (
          <li key={tpl.id}>
            <strong>{tpl.name}</strong> <small>{tpl.description}</small>
            {!tpl.mine && <small> — {t('templates:sharedBy', { name: tpl.owner_name })}</small>}{' '}
            <button type="button" className="primary" title={t('templates:useTip')} onClick={() => use.mutate(tpl.id)} disabled={use.isPending}>
              {t('templates:use')}
            </button>{' '}
            {tpl.mine && (
              <button
                type="button"
                className="danger-button"
                onClick={async () => {
                  if (await ask(t('templates:deleteConfirm', { name: tpl.name }))) remove.mutate(tpl.id)
                }}
                disabled={remove.isPending}
              >
                {t('templates:delete')}
              </button>
            )}
            {tpl.mine && (
              <details>
                <summary>{t('templates:sharing.title')}</summary>
                <label className="legend-item">
                  <input
                    type="checkbox"
                    checked={tpl.shared_all}
                    onChange={(e) => share.mutate({ id: tpl.id, everyone: e.target.checked, groups: tpl.groups })}
                  />{' '}
                  {t('templates:sharing.everyone')}
                </label>
                {(groups.data ?? []).map((g) => (
                  <label key={g.id} className="legend-item">
                    <input
                      type="checkbox"
                      checked={tpl.groups.includes(g.id)}
                      onChange={(e) =>
                        share.mutate({
                          id: tpl.id,
                          everyone: tpl.shared_all,
                          groups: e.target.checked ? [...tpl.groups, g.id] : tpl.groups.filter((x) => x !== g.id),
                        })
                      }
                    />{' '}
                    {t('templates:sharing.group', { name: g.name })}
                  </label>
                ))}
                <p className="hint">{t('templates:sharing.hint')}</p>
              </details>
            )}
          </li>
        ))}
      </ul>
      {(use.isError || remove.isError || share.isError) && <Problem error={use.error ?? remove.error ?? share.error} />}
    </>
  )
}

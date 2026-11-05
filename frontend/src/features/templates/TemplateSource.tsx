import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import type { ProjectSourceProps } from '../../core/registry'

interface Template {
  id: string
  name: string
  description: string
}

// The person's own templates: pick one to start a project from it, or delete it.
export function TemplateSource({ onDraft }: ProjectSourceProps) {
  const { t } = useTranslation(['templates', 'errors'])
  const client = useQueryClient()
  const list = useQuery({ queryKey: ['templates'], queryFn: () => api.get<Template[]>('/templates') })
  const use = useMutation({
    mutationFn: (id: string) => api.get<{ name: string; compose: string }>(`/templates/${id}`),
    onSuccess: (tpl) => onDraft({ name: tpl.name, compose: tpl.compose }),
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/templates/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: ['templates'] }),
  })
  if (list.isError) return <Problem error={list.error} />
  if (list.data?.length === 0) return <p className="empty">{t('templates:none')}</p>
  return (
    <>
      <ul className="links">
        {(list.data ?? []).map((tpl) => (
          <li key={tpl.id}>
            <strong>{tpl.name}</strong> <small>{tpl.description}</small>{' '}
            <button type="button" onClick={() => use.mutate(tpl.id)} disabled={use.isPending}>
              {t('templates:use')}
            </button>{' '}
            <button type="button" onClick={() => remove.mutate(tpl.id)} disabled={remove.isPending}>
              {t('templates:delete')}
            </button>
          </li>
        ))}
      </ul>
      {(use.isError || remove.isError) && <Problem error={use.error ?? remove.error} />}
    </>
  )
}

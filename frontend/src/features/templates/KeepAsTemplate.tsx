import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'
import type { ProjectDraft } from '../../core/registry'

// Keeps the Compose text of the project being created as a template. The .env is not kept:
// it holds values, and a template is meant to be reused.
export function KeepAsTemplate({ draft }: { draft: ProjectDraft }) {
  const { t } = useTranslation(['templates', 'errors'])
  const client = useQueryClient()
  const save = useMutation({
    mutationFn: () => api.post('/templates', { name: draft.name || 'template', description: '', compose: draft.compose }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['templates'] }),
  })
  return (
    <>
      <button type="button" onClick={() => save.mutate()} disabled={save.isPending || !draft.compose.trim()}>
        {t('templates:keep')}
      </button>
      {save.isSuccess && <small> {t('templates:kept')}</small>}
      {save.isError && <Problem error={save.error} />}
    </>
  )
}

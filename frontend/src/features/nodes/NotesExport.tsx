import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

interface NoteRow {
  object_type: string
  object_id: string
  body: string
  updated_at: string
}

// Before a machine goes, offers the person's own notes about it as a Markdown file: once the
// machine is gone for good, its notes go with it. Built in the browser; nothing is stored.
export function NotesExport({ nodeId, nodeName }: { nodeId: string; nodeName: string }) {
  const { t } = useTranslation(['nodes', 'errors'])
  const save = useMutation({
    mutationFn: async () => {
      const notes = await api.get<NoteRow[]>(`/notes?node_id=${encodeURIComponent(nodeId)}`)
      const text = [`# ${nodeName}`, ...notes.map((n) => `\n## ${n.object_type} ${n.object_id}\n\n${n.body}`)].join('\n')
      const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `notes-${nodeId}.md`
      link.click()
      URL.revokeObjectURL(url)
      return notes.length
    },
  })
  return (
    <p>
      <button type="button" onClick={() => save.mutate()} disabled={save.isPending}>
        {t('nodes:delete.exportNotes')}
      </button>{' '}
      {save.isSuccess && <span className="hint">{t('nodes:delete.exported', { count: save.data })}</span>}
      {save.isError && <Problem error={save.error} />}
    </p>
  )
}

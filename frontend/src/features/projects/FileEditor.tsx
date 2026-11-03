import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface FileContent {
  content: string
  sha256: string
  size: number
}

interface Props {
  nodeId: string
  project: string
  file: string
}

// Editor for one Compose file. A save names the version it replaces; if the file changed on
// the machine in the meantime the server answers 409 and nothing is overwritten.
export function FileEditor({ nodeId, project, file }: Props) {
  const { t } = useTranslation(['projects', 'errors'])
  const queryClient = useQueryClient()
  const path = `/nodes/${nodeId}/projects/${encodeURIComponent(project)}/files/${encodeURIComponent(file)}`
  const key = ['project-file', nodeId, project, file]
  const loaded = useQuery({ queryKey: key, queryFn: () => api.get<FileContent>(path), staleTime: Infinity })

  // `draft` is null until the user types: then the editor shows what is on disk.
  const [draft, setDraft] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [saved, setSaved] = useState(false)
  // After "keep mine" the next save knowingly replaces the newer version it was shown.
  const [baseSha, setBaseSha] = useState<string | null>(null)

  const text = draft ?? loaded.data?.content ?? ''
  const dirty = draft !== null && draft !== loaded.data?.content
  const expected = baseSha ?? loaded.data?.sha256 ?? null

  const save = useMutation({
    mutationFn: () => api.put<{ sha256: string; size: number }>(path, { content: text, expected_sha256: expected }),
    onSuccess: async (result) => {
      setConflict(false)
      setSaved(true)
      setBaseSha(null)
      queryClient.setQueryData(key, { content: text, sha256: result.sha256, size: result.size })
      setDraft(null)
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'worker.conflict') setConflict(true)
    },
  })

  async function reloadFromDisk() {
    setDraft(null)
    setConflict(false)
    setBaseSha(null)
    await queryClient.invalidateQueries({ queryKey: key })
  }

  async function keepMine() {
    // Fetch the newer version only to learn its hash: the text stays mine.
    const current = await api.get<FileContent>(path)
    setBaseSha(current.sha256)
    setConflict(false)
  }

  if (loaded.isPending) return <p>{t('loading')}</p>
  if (loaded.isError)
    return (
      <p role="alert" className="error">
        {errorMessage(t, loaded.error)}
      </p>
    )

  return (
    <div>
      <h3>
        {project} / <code>{file}</code> {dirty && <span className="badge">{t('projects:unsaved')}</span>}
      </h3>
      {conflict && (
        <div className="notice" role="alert">
          <p>{t('projects:conflict')}</p>
          <button type="button" onClick={() => void reloadFromDisk()}>
            {t('projects:reload')}
          </button>{' '}
          <button type="button" onClick={() => void keepMine()}>
            {t('projects:keepMine')}
          </button>
        </div>
      )}
      {save.isError && !conflict && (
        <p role="alert" className="error">
          {errorMessage(t, save.error)}
        </p>
      )}
      {saved && !dirty && !conflict && (
        <p role="status" className="ok-text">
          {t('projects:saved')}
        </p>
      )}
      <label htmlFor="file-text" className="visually-hidden">
        {t('projects:editorLabel', { file })}
      </label>
      <textarea
        id="file-text"
        className="editor"
        spellCheck={false}
        value={text}
        onChange={(e) => {
          setDraft(e.target.value)
          setSaved(false)
        }}
      />
      <div className="actions">
        <button type="button" className="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          {t('projects:save')}
        </button>
        {dirty && (
          <button type="button" onClick={() => setDraft(null)}>
            {t('projects:discard')}
          </button>
        )}
      </div>
    </div>
  )
}

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, api } from '../core/api'
import { Problem } from '../components/Problem'

interface FileContent {
  content: string
  sha256: string
  size: number
}

interface Props {
  // API path of the file (it answers GET with the text and PUT to save).
  path: string
  // What is shown as the file's name, and the label of the text area.
  title: string
  // Name of the file alone, for the text area's label (defaults to the title).
  fileName?: string
  queryKey: unknown[]
}

// Editor for one configuration file (a Compose file, a Traefik file...). A save names the version it replaces; if the file changed on
// the machine in the meantime the server answers 409 and nothing is overwritten.
export function FileEditor({ path, title, fileName, queryKey: key }: Props) {
  const { t } = useTranslation(['files', 'errors'])
  const queryClient = useQueryClient()
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
      <Problem error={loaded.error} />
    )

  return (
    <div>
      <h3>
        <code>{title}</code> {dirty && <span className="badge">{t('files:unsaved')}</span>}
      </h3>
      {conflict && (
        <div className="notice" role="alert">
          <p>{t('files:conflict')}</p>
          <button type="button" onClick={() => void reloadFromDisk()}>
            {t('files:reload')}
          </button>{' '}
          <button type="button" onClick={() => void keepMine()}>
            {t('files:keepMine')}
          </button>
        </div>
      )}
      {save.isError && !conflict && (
        <Problem error={save.error} />
      )}
      {saved && !dirty && !conflict && (
        <p role="status" className="ok-text">
          {t('files:saved')}
        </p>
      )}
      <label htmlFor="file-text" className="visually-hidden">
        {t('files:editorLabel', { file: fileName ?? title })}
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
          {t('files:save')}
        </button>
        {dirty && (
          <button type="button" onClick={() => setDraft(null)}>
            {t('files:discard')}
          </button>
        )}
      </div>
    </div>
  )
}

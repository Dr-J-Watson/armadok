import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Folder, FolderOpen, File } from 'lucide-react'
import { Problem } from '../../components/Problem'
import { api } from '../../core/api'

interface Listing {
  kind: 'dir' | 'file'
  entries: { name: string; project: boolean }[]
  truncated: boolean
}

// Walks the folders under a root, one level at a time, as the worker lists them. Choosing is
// the only way to fill the field from here, so a name cannot be mistyped: the worker still
// checks the final value when it is saved. A project folder is shown but cannot be entered.
export function FolderPicker({
  nodeId,
  place,
  root,
  start,
  onPick,
}: {
  nodeId: string
  place: string
  root: string
  start: string
  onPick: (value: string) => void
}) {
  const { t } = useTranslation(['nodeconfig', 'errors'])
  const [path, setPath] = useState(start)
  const listing = useQuery({
    queryKey: ['node-browse', nodeId, place, path],
    queryFn: () => api.get<Listing>(`/nodes/${nodeId}/settings/browse?place=${place}&path=${encodeURIComponent(path)}`),
    retry: false,
  })
  const parts = path.split('/').filter(Boolean)

  if (listing.isError) return <Problem error={listing.error} />
  const data = listing.data
  return (
    <div className="picker" role="group" aria-label={t('nodeconfig:browse')}>
      {data?.kind === 'file' ? (
        <p className="hint">{t('nodeconfig:pickFile')}</p>
      ) : (
        <nav className="crumbs" aria-label={t('nodeconfig:where')}>
          <button type="button" className="link-button" onClick={() => setPath('')}>
            {root}
          </button>
          {parts.map((part, index) => (
            <span key={part + index}>
              {' / '}
              <button type="button" className="link-button" onClick={() => setPath(parts.slice(0, index + 1).join('/'))}>
                {part}
              </button>
            </span>
          ))}
        </nav>
      )}
      {listing.isPending && <p>{t('loading')}</p>}
      {data && data.entries.length === 0 && <p className="empty">{t('nodeconfig:emptyFolder')}</p>}
      <ul className="picker-list">
        {data?.entries.map((entry) =>
          data.kind === 'file' ? (
            <li key={entry.name}>
              <button type="button" onClick={() => onPick(entry.name)}>
                <File size={16} aria-hidden="true" /> {entry.name}
              </button>
            </li>
          ) : (
            <li key={entry.name}>
              <button type="button" disabled={entry.project} onClick={() => setPath(path ? `${path}/${entry.name}` : entry.name)}>
                <Folder size={16} aria-hidden="true" /> {entry.name}
                {entry.project && <small> ({t('nodeconfig:isProject')})</small>}
              </button>
            </li>
          ),
        )}
      </ul>
      {data?.truncated && <p className="hint">{t('nodeconfig:truncated')}</p>}
      {data?.kind === 'dir' && (
        <button type="button" className="primary" onClick={() => onPick(path)}>
          <FolderOpen size={16} aria-hidden="true" /> {path ? t('nodeconfig:useFolder', { path }) : t('nodeconfig:useRoot')}
        </button>
      )}
    </div>
  )
}

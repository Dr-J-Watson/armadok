// "Relations" panel of one object: everything it is linked to, in both directions.
// Each neighbour is a button that moves the selection there.

import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { useFeatures } from '../../core/useFeatures'
import { objectKey } from './graph'
import type { RelationObject } from './graph'
import { Problem } from '../../components/Problem'

interface Panel {
  object: RelationObject
  links: { direction: 'outgoing' | 'incoming'; kind: string; object: RelationObject }[]
}

interface Props {
  nodeId: string
  object: RelationObject
  onSelect: (key: string) => void
  // Where this kind of object has its own screen, if any (e.g. containers).
  extra?: React.ReactNode
}

export function RelationsPanel({ nodeId, object, onSelect, extra }: Props) {
  const { t } = useTranslation(['relations', 'errors'])
  const sections = useFeatures().flatMap((f) => f.objectSections ?? [])
  const panel = useQuery({
    queryKey: ['relations', nodeId, object.type, object.id],
    queryFn: () =>
      api.get<Panel>(
        `/nodes/${nodeId}/relations/${encodeURIComponent(object.type)}/${object.id
          .split('/')
          .map(encodeURIComponent)
          .join('/')}`,
      ),
  })

  return (
    <aside className="panel" aria-live="polite">
      <h2>
        {object.label} <span className="badge">{t(`relations:types.${object.type}`, { defaultValue: object.type })}</span>
      </h2>
      {extra}
      {panel.isPending && <p className="loading">{t('loading')}</p>}
      {panel.isError && (
        <Problem error={panel.error} />
      )}
      {panel.data && panel.data.links.length === 0 && <p className="empty">{t('relations:noLinks')}</p>}
      {panel.data && panel.data.links.length > 0 && (
        <ul className="links">
          {panel.data.links.map((link) => (
            <li key={`${link.direction}:${link.kind}:${objectKey(link.object)}`}>
              <span className="muted">
                {t(`relations:direction.${link.direction}`)} · {t(`relations:kinds.${link.kind}`, { defaultValue: link.kind })}
              </span>{' '}
              <button type="button" className="link-button" onClick={() => onSelect(objectKey(link.object))}>
                {link.object.label}
              </button>{' '}
              <span className="badge">{t(`relations:types.${link.object.type}`, { defaultValue: link.object.type })}</span>
            </li>
          ))}
        </ul>
      )}
      {sections.map((section) => (
        <div key={section.id}>
          {section.render({ nodeId, objectType: object.type, objectId: object.id, objectLabel: object.label })}
        </div>
      ))}
    </aside>
  )
}

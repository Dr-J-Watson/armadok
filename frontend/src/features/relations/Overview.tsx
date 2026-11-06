// The overview of a node: one card per project, with the addresses that reach it and, for each
// container, the image, networks and volumes it uses. Every name is a button that selects the
// object, so the side panel and the small graph follow it.

import { ArrowRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { objectKey } from './graph'
import type { Overview as OverviewData, RelationObject } from './graph'

interface Props {
  data: OverviewData
  selected: string | null
  onSelect: (key: string) => void
  // Which objects match the search; null when nothing is searched.
  matching: Set<string> | null
  // What the selected object is linked to: lit up in the cards.
  linked: Set<string>
}

function Chip({ object, selected, onSelect, quiet, linked }: { object: RelationObject; selected: string | null; onSelect: (key: string) => void; quiet?: boolean; linked: boolean }) {
  const { t } = useTranslation('relations')
  const key = objectKey(object)
  return (
    <button
      type="button"
      className={`chip${quiet ? ' chip-quiet' : ''}`}
      data-type={object.type}
      data-linked={linked || undefined}
      aria-pressed={selected === key}
      title={`${t(`types.${object.type}`, { defaultValue: object.type })} · ${object.label}`}
      onClick={() => onSelect(key)}
    >
      {object.label}
    </button>
  )
}

export function Overview({ data, selected, onSelect, matching, linked }: Props) {
  const { t } = useTranslation('relations')
  const shown = (objects: RelationObject[]) => (matching ? objects.filter((o) => matching.has(objectKey(o))) : objects)
  const chip = (o: RelationObject, quiet = false) => <Chip key={objectKey(o)} object={o} selected={selected} onSelect={onSelect} quiet={quiet} linked={linked.has(objectKey(o))} />
  // A card stays when the search matches anything in it.
  const keepCard = (card: OverviewData['projects'][number]) =>
    !matching ||
    (card.project && matching.has(objectKey(card.project))) ||
    card.containers.some((c) => [c.container, ...c.images, ...c.networks, ...c.volumes].some((o) => matching.has(objectKey(o)))) ||
    card.sites.some((s) => [s.router, ...s.domains, ...s.middlewares].some((o) => matching.has(objectKey(o))))
  const cards = data.projects.filter(keepCard)
  const loose = data.loose.map((g) => ({ ...g, objects: shown(g.objects) })).filter((g) => g.objects.length > 0)

  return (
    <div className="overview">
      {cards.map((card) => (
        <article key={card.project ? objectKey(card.project) : 'none'} className="proj-card">
          <header>
            <h3>{card.project ? chip(card.project) : t('overview.noProject')}</h3>
            <span className="hint">{t('overview.containers', { count: card.containers.length })}</span>
          </header>
          {card.sites.length > 0 && (
            <section aria-label={t('overview.sites')}>
              <h4>{t('overview.sites')}</h4>
              {card.sites.map((site) => (
                <div key={objectKey(site.router)} className="flow">
                  {site.domains.length > 0 ? site.domains.map((d) => chip(d)) : <span className="hint">{t('overview.noDomain')}</span>}
                  <ArrowRight size={14} aria-hidden="true" className="flow-arrow" />
                  {chip(site.router)}
                  {site.middlewares.map((m) => chip(m, true))}
                </div>
              ))}
            </section>
          )}
          {card.containers.length > 0 && (
            <section aria-label={t('overview.containersTitle')}>
              <h4>{t('overview.containersTitle')}</h4>
              {card.containers.map((c) => (
                <div key={objectKey(c.container)} className="container-row">
                  {chip(c.container)}
                  <span className="uses">
                    {c.images.map((o) => chip(o, true))}
                    {c.networks.map((o) => chip(o, true))}
                    {c.volumes.map((o) => chip(o, true))}
                  </span>
                </div>
              ))}
            </section>
          )}
        </article>
      ))}
      {loose.length > 0 && (
        <article className="proj-card proj-loose">
          <header>
            <h3>{t('overview.loose')}</h3>
          </header>
          <p className="hint">{t('overview.looseHint')}</p>
          {loose.map((g) => (
            <details key={g.type}>
              <summary>
                {t(`types.${g.type}`, { defaultValue: g.type })} <span className="badge">{g.objects.length}</span>
              </summary>
              <div className="chip-row">{g.objects.map((o) => chip(o, true))}</div>
            </details>
          ))}
        </article>
      )}
    </div>
  )
}

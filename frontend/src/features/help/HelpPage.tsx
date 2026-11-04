import { useTranslation } from 'react-i18next'

const SECTIONS = ['start', 'words', 'rights', 'safety', 'shortcuts'] as const
const TERMS = ['node', 'worker', 'project', 'site', 'middleware', 'relation', 'note'] as const

// A short guide to the interface, in the person's language: where to start, what the words
// mean, how rights work and what is kept private. The long documentation lives in the README.
export function HelpPage() {
  const { t } = useTranslation('help')
  return (
    <main>
      <h1>{t('title')}</h1>
      <p>{t('intro')}</p>
      <nav aria-label={t('title')}>
        <ul className="links">
          {SECTIONS.map((s) => (
            <li key={s}>
              <a href={`#help-${s}`}>{t(`${s}.title`)}</a>
            </li>
          ))}
        </ul>
      </nav>
      <section id="help-start">
        <h2>{t('start.title')}</h2>
        <ol>
          {[1, 2, 3, 4].map((n) => (
            <li key={n}>{t(`start.step${n}`)}</li>
          ))}
        </ol>
      </section>
      <section id="help-words">
        <h2>{t('words.title')}</h2>
        <dl className="details">
          {TERMS.map((term) => (
            <div key={term}>
              <dt>{t(`words.${term}.name`)}</dt>
              <dd>{t(`words.${term}.text`)}</dd>
            </div>
          ))}
        </dl>
      </section>
      <section id="help-rights">
        <h2>{t('rights.title')}</h2>
        <p>{t('rights.text')}</p>
        <p>{t('rights.sensitive')}</p>
      </section>
      <section id="help-safety">
        <h2>{t('safety.title')}</h2>
        <p>{t('safety.text')}</p>
      </section>
      <section id="help-shortcuts">
        <h2>{t('shortcuts.title')}</h2>
        <p>{t('shortcuts.text')}</p>
      </section>
    </main>
  )
}

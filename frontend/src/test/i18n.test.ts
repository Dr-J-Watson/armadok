import { resources } from '../core/i18n'

// Every text must exist in both languages: a missing key would show an English fallback
// to a French user, or worse, a bare key.
describe('translations', () => {
  const languages = Object.keys(resources)

  it('ships French and English', () => {
    expect(languages.sort()).toEqual(['en', 'fr'])
  })

  it('has the same namespaces and keys in every language', () => {
    const reference = resources.en
    for (const lang of languages) {
      expect(Object.keys(resources[lang]).sort()).toEqual(Object.keys(reference).sort())
      for (const ns of Object.keys(reference)) {
        expect(Object.keys(resources[lang][ns]).sort(), `${lang}/${ns}`).toEqual(
          Object.keys(reference[ns]).sort(),
        )
      }
    }
  })

  it('has no empty text', () => {
    for (const lang of languages)
      for (const [ns, texts] of Object.entries(resources[lang]))
        for (const [key, value] of Object.entries(texts))
          expect(value.trim(), `${lang}/${ns}/${key}`).not.toBe('')
  })

  it('translates every error code the server can send', () => {
    const codes = Object.keys(resources.en.errors)
    for (const code of ['rights.missing', 'auth.invalid_credentials', 'worker.not_connected'])
      expect(codes).toContain(code)
  })
})

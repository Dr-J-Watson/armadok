import { ACCENT_PRESETS, applyAccent, currentAccent, textOn } from '../core/theme'

afterEach(() => {
  applyAccent(null)
  window.localStorage.clear()
})

describe('accent colour', () => {
  it('sets the variables and remembers the choice', () => {
    applyAccent('#c2306a')
    const style = document.documentElement.style
    expect(style.getPropertyValue('--accent')).toBe('#c2306a')
    expect(style.getPropertyValue('--accent-text')).toBe('#ffffff')
    expect(currentAccent()).toBe('#c2306a')
  })

  it('puts dark text on a light accent and light text on a dark one', () => {
    expect(textOn('#ffe066')).toBe('#0b1220')
    expect(textOn('#1a237e')).toBe('#ffffff')
    for (const preset of ACCENT_PRESETS) expect(['#0b1220', '#ffffff']).toContain(textOn(preset))
  })

  it('ignores anything that is not a plain #rrggbb and can go back to the default', () => {
    applyAccent('red; background: url(x)')
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('')
    applyAccent('#123456')
    applyAccent(null)
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('')
    expect(currentAccent()).toBeNull()
  })

  it('does not trust a damaged saved value', () => {
    window.localStorage.setItem('armadok.accent', 'javascript:alert(1)')
    expect(currentAccent()).toBeNull()
  })
})

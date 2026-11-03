// Browser storage for preferences only: never a secret, never a token.
// Every access is wrapped because storage can be blocked (private window, site settings).

const PREFIX = 'armadok.'

export function readPref(key: string): string | null {
  try {
    return window.localStorage.getItem(PREFIX + key)
  } catch {
    return null
  }
}

export function writePref(key: string, value: string): void {
  try {
    window.localStorage.setItem(PREFIX + key, value)
  } catch {
    // Storage unavailable: the preference simply is not remembered.
  }
}

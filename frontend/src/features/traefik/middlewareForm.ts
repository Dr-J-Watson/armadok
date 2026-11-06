// What the middleware form holds, and how it maps to and from the API's body.

export const INTENTS = ['local_only', 'basic_auth', 'security_headers', 'redirect_https', 'strip_prefix', 'rate_limit', 'compress', 'forward_auth'] as const
export type Intent = (typeof INTENTS)[number]

export interface UserRow {
  username: string
  password: string
  has_password?: boolean
}

// What the form holds: every field as text or a simple value, turned into the API's body below.
export interface Form {
  intent: Intent
  name: string
  ranges: string
  users: UserRow[]
  removeHeader: boolean
  hstsSeconds: string
  hstsSubdomains: boolean
  frameDeny: boolean
  nosniff: boolean
  referrer: string
  csp: string
  permanent: boolean
  prefixes: string
  average: string
  burst: string
  period: string
  minBytes: string
  address: string
  trustForward: boolean
  responseHeaders: string
}

export interface Item {
  name: string
  sha256: string
  editable: boolean
  used_by: string[]
  spec: Record<string, unknown> & { intent: Intent; name: string }
}

const PRIVATE = ['127.0.0.1/32', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7']
export const blank = (intent: Intent = 'local_only'): Form => ({
  intent, name: '', ranges: PRIVATE.join('\n'), users: [{ username: '', password: '' }], removeHeader: true,
  hstsSeconds: '31536000', hstsSubdomains: false, frameDeny: true, nosniff: true, referrer: 'strict-origin-when-cross-origin', csp: '',
  permanent: true, prefixes: '', average: '100', burst: '50', period: '1', minBytes: '1024', address: '', trustForward: false, responseHeaders: '',
})

const lines = (text: string) => text.split(/[\n,]/).map((x) => x.trim()).filter(Boolean)

// The API's body for a form. A password travels once, here, and is never shown again.
export function body(f: Form): Record<string, unknown> {
  const base = { intent: f.intent, name: f.name }
  switch (f.intent) {
    case 'local_only': return { ...base, ranges: lines(f.ranges) }
    case 'basic_auth': return { ...base, remove_header: f.removeHeader, users: f.users.map((u) => ({ username: u.username, ...(u.password ? { password: u.password } : {}) })) }
    case 'security_headers': return { ...base, hsts_seconds: Number(f.hstsSeconds), hsts_subdomains: f.hstsSubdomains, frame_deny: f.frameDeny, nosniff: f.nosniff, referrer_policy: f.referrer, csp: f.csp || null }
    case 'redirect_https': return { ...base, permanent: f.permanent }
    case 'strip_prefix': return { ...base, prefixes: lines(f.prefixes) }
    case 'rate_limit': return { ...base, average: Number(f.average), burst: Number(f.burst), period_seconds: Number(f.period) }
    case 'compress': return { ...base, min_bytes: Number(f.minBytes) }
    case 'forward_auth': return { ...base, address: f.address, trust_forward_header: f.trustForward, response_headers: lines(f.responseHeaders) }
  }
}

// The form for an existing middleware (passwords are never part of it: they are empty).
export function fromSpec(spec: Item['spec']): Form {
  const f = blank(spec.intent)
  const s = spec as Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any
  f.name = spec.name
  if (spec.intent === 'local_only') f.ranges = (s.ranges as string[]).join('\n')
  if (spec.intent === 'basic_auth') {
    f.removeHeader = s.remove_header
    f.users = (s.users as { username: string; has_password: boolean }[]).map((u) => ({ username: u.username, password: '', has_password: u.has_password }))
  }
  if (spec.intent === 'security_headers') Object.assign(f, { hstsSeconds: String(s.hsts_seconds), hstsSubdomains: s.hsts_subdomains, frameDeny: s.frame_deny, nosniff: s.nosniff, referrer: s.referrer_policy, csp: s.csp ?? '' })
  if (spec.intent === 'redirect_https') f.permanent = s.permanent
  if (spec.intent === 'strip_prefix') f.prefixes = (s.prefixes as string[]).join('\n')
  if (spec.intent === 'rate_limit') Object.assign(f, { average: String(s.average), burst: String(s.burst), period: String(s.period_seconds) })
  if (spec.intent === 'compress') f.minBytes = String(s.min_bytes)
  if (spec.intent === 'forward_auth') Object.assign(f, { address: s.address, trustForward: s.trust_forward_header, responseHeaders: (s.response_headers as string[]).join(', ') })
  return f
}

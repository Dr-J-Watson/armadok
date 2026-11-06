// The options of Traefik's HTTP middlewares (the dynamic configuration of v3), as a catalogue the
// form is drawn from. Option names are Traefik's own and are not translated.

export type FieldType = 's' | 'n' | 'b' | 'd' | 'e' | 'sl' | 'kv' | 'o' | 'refl' | 'ref'

export interface Field {
  k: string
  t: FieldType
  ph?: string
  opts?: string[]
  fields?: Field[]
  // A group whose fields live in the parent object, not under their own key.
  flat?: boolean
  required?: boolean
  // What a reference points to.
  ref?: 'services' | 'middlewares'
  // A longer explanation exists (key `traefik:schema.help.<k>`).
  help?: boolean
  hash?: boolean
}

const f = (k: string, t: FieldType, extra: Partial<Field> = {}): Field => ({ k, t, ...extra })
const s = (k: string, extra?: Partial<Field>) => f(k, 's', extra)
const n = (k: string, extra?: Partial<Field>) => f(k, 'n', extra)
const b = (k: string, extra?: Partial<Field>) => f(k, 'b', extra)
const d = (k: string, ph: string, extra?: Partial<Field>) => f(k, 'd', { ph, ...extra })
const e = (k: string, opts: string[], extra?: Partial<Field>) => f(k, 'e', { opts, ...extra })
const sl = (k: string, extra?: Partial<Field>) => f(k, 'sl', extra)
const kv = (k: string, extra?: Partial<Field>) => f(k, 'kv', extra)
const o = (k: string, fields: Field[], extra?: Partial<Field>) => f(k, 'o', { fields, ...extra })

const ipStrategy = o('ipStrategy', [n('depth', { help: true }), sl('excludedIPs', { ph: '10.0.0.1' }), s('ipv6Subnet')])
const sourceCriterion = o('sourceCriterion', [ipStrategy, s('requestHeaderName'), b('requestHost')])
const certInfo = (k: string) => o(k, ['country', 'province', 'locality', 'organization', 'commonName', 'serialNumber', 'domainComponent'].map((x) => b(x)))

export interface MiddlewareType {
  fields: Field[]
  // The whole configuration is free-form (plugins): only the YAML view makes sense.
  yamlOnly?: boolean
}

export const MIDDLEWARES: Record<string, MiddlewareType> = {
  basicAuth: { fields: [sl('users', { ph: 'user:$2a$12$…', hash: true, help: true }), s('usersFile'), s('realm'), b('removeHeader'), s('headerField')] },
  digestAuth: { fields: [sl('users'), s('usersFile'), s('realm'), b('removeHeader'), s('headerField')] },
  forwardAuth: { fields: [s('address', { required: true, ph: 'http://authelia:9091/api/verify' }), b('trustForwardHeader'), sl('authResponseHeaders'), s('authResponseHeadersRegex'), sl('authRequestHeaders'), b('addAuthCookiesToResponse'), o('tls', [s('ca'), s('cert'), s('key'), b('insecureSkipVerify')])] },
  ipAllowList: { fields: [sl('sourceRange', { required: true, ph: '192.168.0.0/16' }), ipStrategy] },
  ipWhiteList: { fields: [sl('sourceRange'), ipStrategy] },
  passTLSClientCert: { fields: [b('pem'), o('info', [b('notAfter'), b('notBefore'), b('sans'), b('serialNumber'), certInfo('subject'), certInfo('issuer')])] },
  headers: {
    fields: [
      kv('customRequestHeaders', { help: true }),
      kv('customResponseHeaders'),
      o('browser', [n('stsSeconds', { ph: '31536000' }), b('stsIncludeSubdomains'), b('stsPreload'), b('forceSTSHeader'), b('frameDeny'), s('customFrameOptionsValue'), b('contentTypeNosniff'), b('browserXssFilter'), s('customBrowserXSSValue'), s('contentSecurityPolicy'), s('contentSecurityPolicyReportOnly'), s('referrerPolicy', { ph: 'strict-origin-when-cross-origin' }), s('permissionsPolicy'), b('isDevelopment')], { flat: true }),
      o('cors', [sl('accessControlAllowOriginList'), sl('accessControlAllowOriginListRegex'), sl('accessControlAllowMethods'), sl('accessControlAllowHeaders'), sl('accessControlExposeHeaders'), b('accessControlAllowCredentials'), n('accessControlMaxAge'), b('addVaryHeader')], { flat: true }),
      o('hosts', [sl('allowedHosts'), kv('hostsProxyHeaders'), kv('sslProxyHeaders')], { flat: true }),
    ],
  },
  addPrefix: { fields: [s('prefix', { required: true, ph: '/api' })] },
  stripPrefix: { fields: [sl('prefixes', { required: true, ph: '/app' }), b('forceSlash')] },
  stripPrefixRegex: { fields: [sl('regex', { required: true })] },
  replacePath: { fields: [s('path', { required: true })] },
  replacePathRegex: { fields: [s('regex', { required: true }), s('replacement', { required: true })] },
  redirectScheme: { fields: [e('scheme', ['https', 'http']), s('port'), b('permanent')] },
  redirectRegex: { fields: [s('regex', { required: true, ph: '^https://(.*)/old/(.*)' }), s('replacement', { required: true, ph: 'https://${1}/new/${2}' }), b('permanent')] },
  compress: { fields: [sl('excludedContentTypes'), sl('includedContentTypes'), n('minResponseBodyBytes', { ph: '1024' }), sl('encodings'), s('defaultEncoding')] },
  contentType: { fields: [b('autoDetect')] },
  buffering: { fields: [n('maxRequestBodyBytes'), n('memRequestBodyBytes'), n('maxResponseBodyBytes'), n('memResponseBodyBytes'), s('retryExpression')] },
  errors: { fields: [sl('status', { required: true, ph: '500-599' }), f('service', 'ref', { ref: 'services', required: true }), s('query', { ph: '/{status}.html' })] },
  chain: { fields: [f('middlewares', 'refl', { ref: 'middlewares', required: true, help: true })] },
  grpcWeb: { fields: [sl('allowOrigins')] },
  rateLimit: { fields: [n('average', { required: true }), d('period', '1s'), n('burst'), sourceCriterion] },
  inFlightReq: { fields: [n('amount', { required: true }), sourceCriterion] },
  retry: { fields: [n('attempts', { required: true }), d('initialInterval', '100ms')] },
  circuitBreaker: { fields: [s('expression', { required: true, ph: 'NetworkErrorRatio() > 0.30' }), d('checkPeriod', '100ms'), d('fallbackDuration', '10s'), d('recoveryDuration', '10s'), n('responseCode', { ph: '503' })] },
  plugin: { fields: [], yamlOnly: true },
}

// A starting point for each common case of the gallery: the type it makes and what is filled in.
export const PRESETS: Record<string, { type: string; name: string; value: Record<string, unknown> }> = {
  local_only: { type: 'ipAllowList', name: 'local-only', value: { ipAllowList: { sourceRange: ['192.168.0.0/16', '10.0.0.0/8', '172.16.0.0/12'] } } },
  basic_auth: { type: 'basicAuth', name: 'auth', value: { basicAuth: { users: [] } } },
  forward_auth: { type: 'forwardAuth', name: 'sso', value: { forwardAuth: { address: '', trustForwardHeader: true } } },
  security_headers: { type: 'headers', name: 'secure-headers', value: { headers: { stsSeconds: 63072000, stsIncludeSubdomains: true, stsPreload: true, contentTypeNosniff: true, frameDeny: true } } },
  redirect_https: { type: 'redirectScheme', name: 'https-redirect', value: { redirectScheme: { scheme: 'https', permanent: true } } },
  strip_prefix: { type: 'stripPrefix', name: 'strip-prefix', value: { stripPrefix: { prefixes: [] } } },
  rate_limit: { type: 'rateLimit', name: 'rate-limit', value: { rateLimit: { average: 100, burst: 50 } } },
  compress: { type: 'compress', name: 'compress', value: { compress: {} } },
}

// Drops what is empty (text, lists, objects) so that the file only holds what was set; `false` and
// `0` are real values and stay.
export function prune(value: unknown): unknown {
  if (Array.isArray(value)) {
    const items = value.map(prune).filter((x) => x !== undefined)
    return items.length > 0 ? items : undefined
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => [k, prune(v)] as const)
      .filter(([, v]) => v !== undefined)
    return entries.length > 0 ? Object.fromEntries(entries) : undefined
  }
  if (value === '' || value === null || value === undefined) return undefined
  return value
}

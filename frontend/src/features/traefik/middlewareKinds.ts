// How the page groups Traefik's middleware types: a group (for the sections of the page) and an
// icon. The names of the types themselves are Traefik's and are never translated.

export type MiddlewareGroup = 'security' | 'path' | 'content' | 'traffic' | 'group' | 'other'

export const GROUPS: MiddlewareGroup[] = ['security', 'path', 'content', 'traffic', 'group', 'other']

export const KINDS: Record<string, { group: MiddlewareGroup; icon: string; label: string }> = {
  basicAuth: { group: 'security', icon: 'KeyRound', label: 'Basic Auth' },
  digestAuth: { group: 'security', icon: 'KeyRound', label: 'Digest Auth' },
  forwardAuth: { group: 'security', icon: 'ShieldCheck', label: 'Forward Auth' },
  ipAllowList: { group: 'security', icon: 'Network', label: 'IP Allow List' },
  ipWhiteList: { group: 'security', icon: 'Network', label: 'IP White List' },
  passTLSClientCert: { group: 'security', icon: 'FileKey', label: 'Pass TLS Client Cert' },
  headers: { group: 'security', icon: 'Shield', label: 'Headers' },
  redirectScheme: { group: 'path', icon: 'CornerUpRight', label: 'Redirect Scheme' },
  redirectRegex: { group: 'path', icon: 'CornerUpRight', label: 'Redirect Regex' },
  addPrefix: { group: 'path', icon: 'FolderPlus', label: 'Add Prefix' },
  stripPrefix: { group: 'path', icon: 'FolderMinus', label: 'Strip Prefix' },
  stripPrefixRegex: { group: 'path', icon: 'FolderMinus', label: 'Strip Prefix Regex' },
  replacePath: { group: 'path', icon: 'Replace', label: 'Replace Path' },
  replacePathRegex: { group: 'path', icon: 'Replace', label: 'Replace Path Regex' },
  compress: { group: 'content', icon: 'Package', label: 'Compress' },
  contentType: { group: 'content', icon: 'FileType', label: 'Content Type' },
  buffering: { group: 'content', icon: 'Database', label: 'Buffering' },
  errors: { group: 'content', icon: 'TriangleAlert', label: 'Custom Errors' },
  grpcWeb: { group: 'content', icon: 'Plug', label: 'gRPC-Web' },
  rateLimit: { group: 'traffic', icon: 'Gauge', label: 'Rate Limit' },
  inFlightReq: { group: 'traffic', icon: 'Gauge', label: 'In-Flight Requests' },
  retry: { group: 'traffic', icon: 'RotateCw', label: 'Retry' },
  circuitBreaker: { group: 'traffic', icon: 'Zap', label: 'Circuit Breaker' },
  chain: { group: 'group', icon: 'Link2', label: 'Chain' },
  plugin: { group: 'other', icon: 'Puzzle', label: 'Plugin' },
}

export const kindOf = (type: string) => KINDS[type] ?? { group: 'other' as MiddlewareGroup, icon: 'Box', label: type || 'Middleware' }

// The middleware types the form can make, with the type each one produces (for its icon and
// colour in the gallery).
export const INTENT_TYPE: Record<string, string> = {
  local_only: 'ipAllowList',
  basic_auth: 'basicAuth',
  forward_auth: 'forwardAuth',
  security_headers: 'headers',
  redirect_https: 'redirectScheme',
  strip_prefix: 'stripPrefix',
  rate_limit: 'rateLimit',
  compress: 'compress',
}

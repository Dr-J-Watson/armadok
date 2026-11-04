// The only way the interface talks to the server: the REST API.
//
// The short-lived access token lives in memory only. The long-lived refresh token is an
// HttpOnly cookie the browser sends to /auth endpoints by itself; script never sees it.

export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(code: string, status: number) {
    super(code)
    this.code = code
    this.status = status
  }
}

const BASE = '/api/v1'
let accessToken: string | null = null
let refreshing: Promise<boolean> | null = null

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function currentAccessToken(): string | null {
  return accessToken
}

export function hasAccessToken(): boolean {
  return accessToken !== null
}

async function send(path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers)
  if (init.body !== undefined) headers.set('Content-Type', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  return fetch(BASE + path, { ...init, headers, credentials: 'same-origin' })
}

// Several requests can fail at once with an expired token: they share one refresh, because a
// refresh token works only once.
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      const response = await fetch(`${BASE}/auth/refresh`, {
        method: 'POST',
        credentials: 'same-origin',
      })
      if (!response.ok) {
        accessToken = null
        return false
      }
      accessToken = ((await response.json()) as { access_token: string }).access_token
      return true
    } catch {
      return false
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
  }
  let response = await send(path, init)
  // A wrong second-factor code also answers 401: it must not be sent a second time (it would
  // use up two of the few attempts allowed), so only an expired session is retried.
  if (
    response.status === 401 &&
    !path.startsWith('/auth/') &&
    (await errorCode(response.clone())) !== 'auth.invalid_code' &&
    (await refreshSession())
  ) {
    response = await send(path, init)
  }
  if (!response.ok) {
    let code = 'common.unknown'
    try {
      code = ((await response.json()) as { error?: { code?: string } }).error?.code ?? code
    } catch {
      // Not JSON (a proxy error page, say): keep the generic code.
    }
    throw new ApiError(code, response.status)
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T)
}

async function errorCode(response: Response): Promise<string> {
  try {
    return ((await response.json()) as { error?: { code?: string } }).error?.code ?? 'common.unknown'
  } catch {
    return 'common.unknown' // not JSON (a proxy error page, say)
  }
}

// Download a file the API produces (an export): same authentication as any other call, then the
// browser is asked to save the result. Nothing is kept in memory longer than the click.
export async function download(path: string, filename: string): Promise<void> {
  let response = await send(path, { method: 'GET' })
  if (response.status === 401 && (await refreshSession())) response = await send(path, { method: 'GET' })
  if (!response.ok) throw new ApiError('common.unknown', response.status)
  save(await response.blob(), filename)
}

// Hand a file to the browser. The temporary address is released a little later: some browsers
// cancel the download if it disappears right after the click.
function save(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  del: <T = void>(path: string, body?: unknown) => request<T>('DELETE', path, body),
}

// Like `download`, for a file that needs a body to be released (a fresh second-factor code).
export async function downloadWith(path: string, body: unknown, filename: string): Promise<void> {
  const init = { method: 'POST', body: JSON.stringify(body) }
  let response = await send(path, init)
  // Only an expired session is retried: a wrong code answers 401 too, and sending it again
  // would use up two of the few attempts allowed.
  if (response.status === 401 && (await errorCode(response.clone())) === 'auth.required' && (await refreshSession())) {
    response = await send(path, init)
  }
  if (!response.ok) throw new ApiError(await errorCode(response), response.status)
  save(await response.blob(), filename)
}

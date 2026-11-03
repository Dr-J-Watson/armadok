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
  if (response.status === 401 && !path.startsWith('/auth/') && (await refreshSession())) {
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

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  del: <T = void>(path: string) => request<T>('DELETE', path),
}

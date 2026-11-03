import { ApiError, api, setAccessToken } from '../core/api'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status })
}

afterEach(() => {
  vi.restoreAllMocks()
  setAccessToken(null)
})

describe('api client', () => {
  it('turns a server error into an ApiError carrying its code', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ error: { code: 'rights.missing' } }, 403))
    await expect(api.get('/nodes')).rejects.toMatchObject({ code: 'rights.missing', status: 403 })
  })

  it('falls back to a generic code when the error is not JSON', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('<html>', { status: 502 }))
    await expect(api.get('/nodes')).rejects.toBeInstanceOf(ApiError)
    await expect(api.get('/nodes')).rejects.toMatchObject({ code: 'common.unknown' })
  })

  it('refreshes once when the token expired and retries the request', async () => {
    setAccessToken('old')
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ error: { code: 'auth.required' } }, 401))
      .mockResolvedValueOnce(json({ access_token: 'fresh' }))
      .mockResolvedValueOnce(json([{ id: 'n1' }]))
    await expect(api.get('/nodes')).resolves.toEqual([{ id: 'n1' }])
    const retry = fetchMock.mock.calls[2][1] as RequestInit
    expect(new Headers(retry.headers).get('Authorization')).toBe('Bearer fresh')
  })

  it('shares one refresh between simultaneous failures (a refresh token works once)', async () => {
    setAccessToken('old')
    let refreshes = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/auth/refresh')) {
        refreshes += 1
        return json({ access_token: 'fresh' })
      }
      const auth = new Headers((init as RequestInit).headers).get('Authorization')
      return auth === 'Bearer fresh' ? json({ ok: true }) : json({ error: { code: 'auth.required' } }, 401)
    })
    await Promise.all([api.get('/a'), api.get('/b'), api.get('/c')])
    expect(refreshes).toBe(1)
  })
})

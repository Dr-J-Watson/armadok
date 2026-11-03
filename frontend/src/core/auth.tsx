// Session state of the interface. Tokens never reach storage: the access token is kept in
// memory by `api.ts` and the refresh token is an HttpOnly cookie.

import { useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, refreshSession, setAccessToken } from './api'
import { stillValid } from './time'

export interface Grant {
  right: string
  scope: string
  expires_at: string | null
}

export interface User {
  id: string
  username: string
  grants: Grant[]
  totp_enabled: boolean
  language?: string
  backup_codes_remaining: number
}

type State =
  | { status: 'loading' }
  | { status: 'setup' }
  | { status: 'anonymous' }
  | { status: 'authenticated'; user: User }

interface AuthContextValue {
  state: State
  login: (username: string, password: string, totpCode?: string) => Promise<void>
  setup: (installCode: string, username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>({ status: 'loading' })
  const queryClient = useQueryClient()

  const loadUser = useCallback(async () => {
    const user = await api.get<User>('/auth/me')
    setState({ status: 'authenticated', user })
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        const { setup_required } = await api.get<{ setup_required: boolean }>('/setup/status')
        if (setup_required) return setState({ status: 'setup' })
        if (await refreshSession()) return await loadUser()
      } catch {
        // Server unreachable or session invalid: show the login page.
      }
      setState({ status: 'anonymous' })
    })()
  }, [loadUser])

  const login = useCallback(
    async (username: string, password: string, totpCode?: string) => {
      const body: Record<string, string> = { username, password }
      if (totpCode) body.totp_code = totpCode
      const { access_token } = await api.post<{ access_token: string }>('/auth/login', body)
      setAccessToken(access_token)
      await loadUser()
    },
    [loadUser],
  )

  const setup = useCallback(
    async (installCode: string, username: string, password: string) => {
      await api.post('/setup', { install_code: installCode, username, password })
      await login(username, password)
    },
    [login],
  )

  const logout = useCallback(async () => {
    try {
      await api.post('/auth/logout')
    } finally {
      setAccessToken(null)
      queryClient.clear() // nothing of the previous account may stay in the cache
      setState({ status: 'anonymous' })
    }
  }, [queryClient])

  const value = useMemo(() => ({ state, login, setup, logout }), [state, login, setup, logout])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}

// Interface hint only: whether the account holds a right everywhere, to hide screens it could
// not use anyway. The server decides for real on every request.
export function useCan(right: string): boolean {
  return useCanFn()(right)
}

// The same hint as a function, for code that checks several rights (a list of tabs).
export function useCanFn(): (right: string) => boolean {
  const { state } = useAuth()
  return (right) =>
    state.status === 'authenticated' &&
    state.user.grants.some((g) => g.right === right && g.scope === '*' && stillValid(g.expires_at))
}

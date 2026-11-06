import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { startRealtime } from './realtime'
import type { RealtimeStatus } from './realtime'
import { useFeatures } from './useFeatures'

const StatusContext = createContext<RealtimeStatus>('offline')

// Keeps the live connection open while someone is logged in, and shares its state.
export function RealtimeProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const features = useFeatures()
  const [status, setStatus] = useState<RealtimeStatus>('connecting')
  // The connection must not restart when the list of features changes (it does once, when the
  // modules arrive): it reads the current list when a signal comes in instead.
  const current = useRef(features)
  useEffect(() => {
    current.current = features
  }, [features])
  useEffect(
    () =>
      startRealtime({
        queryClient,
        get features() {
          return current.current
        },
        onStatus: setStatus,
      }),
    [queryClient],
  )
  return <StatusContext.Provider value={status}>{children}</StatusContext.Provider>
}

export function useRealtimeStatus(): RealtimeStatus {
  return useContext(StatusContext)
}

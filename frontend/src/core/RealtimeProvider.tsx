import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useState } from 'react'
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
  useEffect(
    () => startRealtime({ queryClient, features, onStatus: setStatus }),
    [queryClient, features],
  )
  return <StatusContext.Provider value={status}>{children}</StatusContext.Provider>
}

export function useRealtimeStatus(): RealtimeStatus {
  return useContext(StatusContext)
}

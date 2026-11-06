// Which backend modules are enabled: the interface only shows features whose module exists.

import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { api } from './api'

export function useModules(): Set<string> | undefined {
  const { data } = useQuery({
    queryKey: ['modules'],
    queryFn: () => api.get<{ modules: string[] }>('/modules'),
    staleTime: Infinity,
  })
  // One Set per answer, not one per render: callers use it as a dependency, and a fresh object
  // each time would restart the live connection and reload every screen in a loop.
  return useMemo(() => (data ? new Set(data.modules) : undefined), [data])
}

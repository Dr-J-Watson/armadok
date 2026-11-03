// Which backend modules are enabled: the interface only shows features whose module exists.

import { useQuery } from '@tanstack/react-query'
import { api } from './api'

export function useModules(): Set<string> | undefined {
  const { data } = useQuery({
    queryKey: ['modules'],
    queryFn: () => api.get<{ modules: string[] }>('/modules'),
    staleTime: Infinity,
  })
  return data ? new Set(data.modules) : undefined
}

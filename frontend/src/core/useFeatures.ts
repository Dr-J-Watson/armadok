// The features that are enabled: those whose backend module exists.

import { useMemo } from 'react'
import { features } from '../features/enabled'
import type { Feature } from './registry'
import { useModules } from './modules'

export function useFeatures(): Feature[] {
  const modules = useModules()
  return useMemo(
    () => (modules ? features.filter((f) => !f.requiresModule || modules.has(f.requiresModule)) : []),
    [modules],
  )
}

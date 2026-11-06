import { useMemo } from 'react'
import { useCanFn } from '../../core/auth'
import { useModules } from '../../core/modules'
import type { NodeTab } from '../../core/registry'
import { features } from '../enabled'
import type { Node } from './types'

// The tabs of a node that this account may open: those a feature contributes, whose module is
// switched on, that apply to the node's type, and whose right the account holds. The side menu
// and the node page both read this list, so they never disagree.
export function useNodeTabs(node: Node | undefined): NodeTab[] {
  const modules = useModules()
  const can = useCanFn()
  return useMemo(() => {
    if (!node || !modules) return []
    return features
      .filter((f) => !f.requiresModule || modules.has(f.requiresModule))
      .flatMap((f) => f.nodeTabs ?? [])
      .filter((tab) => !tab.nodeTypes || tab.nodeTypes.includes(node.type))
      .filter((tab) => !tab.requiresRight || can(tab.requiresRight))
  }, [node, modules, can])
}

export type Mode = 'docker' | 'network'

// The modes a node offers, from its type: a "full" node has both, the others only their own.
export function modesOf(node: Node): Mode[] {
  if (node.type === 'full') return ['docker', 'network']
  return [node.type]
}

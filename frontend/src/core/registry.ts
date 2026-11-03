// One feature = one folder under `features/` that exports a `Feature` and is listed once in
// `features/enabled.ts`. The shell reads these declarations; it never names a feature.

import type { ReactNode } from 'react'
import type { RouteObject } from 'react-router-dom'

export interface NavItem {
  id: string
  labelKey: string // in the `common` namespace
  to: string
}

export interface NodeTabProps {
  nodeId: string
  nodeType: string
}

// A tab of the node page, contributed by a feature.
export interface NodeTab {
  id: string
  labelKey: string // in the feature's own namespace, e.g. "docker:tab"
  // Node types that get this tab (omit for all).
  nodeTypes?: string[]
  render: (props: NodeTabProps) => ReactNode
}

export interface Feature {
  name: string
  // Backend module that must be enabled for this feature to appear.
  requiresModule?: string
  routes?: RouteObject[]
  nav?: NavItem[]
  nodeTabs?: NodeTab[]
}

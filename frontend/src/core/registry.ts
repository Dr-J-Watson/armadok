// One feature = one folder under `features/` that exports a `Feature` and is listed once in
// `features/enabled.ts`. The shell reads these declarations; it never names a feature.

import type { ReactNode } from 'react'
import type { RouteObject } from 'react-router-dom'

export interface NavItem {
  id: string
  labelKey: string // in the `common` namespace
  to: string
  // Hidden when the account does not hold this right everywhere (the server still decides).
  requiresRight?: string
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

// A section shown for any object (node, container, router...), contributed by a feature: the
// notes panel is one. Objects are identified by their node, a type and an id.
export interface ObjectSectionProps {
  nodeId: string
  objectType: string
  objectId: string
  objectLabel: string
}

export interface ObjectSection {
  id: string
  render: (props: ObjectSectionProps) => ReactNode
}

export interface Feature {
  name: string
  // Backend module that must be enabled for this feature to appear.
  requiresModule?: string
  routes?: RouteObject[]
  nav?: NavItem[]
  nodeTabs?: NodeTab[]
  objectSections?: ObjectSection[]
}

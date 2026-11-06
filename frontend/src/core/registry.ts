// One feature = one folder under `features/` that exports a `Feature` and is listed once in
// `features/enabled.ts`. The shell reads these declarations; it never names a feature.

import type { ReactNode } from 'react'
import type { RouteObject } from 'react-router-dom'

export interface NavItem {
  id: string
  labelKey: string // in the `common` namespace
  to: string
  // Which group of the side menu it sits in (`nav.section.<id>` in `common`); omitted = main.
  section?: 'main' | 'access' | 'admin' | 'personal'
  // A name from `components/NavIcon` (omitted = a plain dot).
  icon?: string
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
  // Where it lives: in the Docker menu, in the network menu, among the node's own pages (map,
  // resources), or - when omitted - in the node's settings (members, rights, worker...).
  area?: 'docker' | 'network' | 'node'
  // A name from `components/NavIcon`, for the side menu.
  icon?: string
  // Node types that get this tab (omit for all).
  nodeTypes?: string[]
  // Hidden when the account does not hold this right everywhere (the server still decides).
  requiresRight?: string
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

// A small piece of the top bar (a bell, a status light), contributed by a feature.
export interface HeaderWidget {
  id: string
  render: () => ReactNode
}

// What a new project starts from. A feature that can produce a Compose text (a template, a
// converted `docker run`, running containers...) contributes a source; the project creation
// screen lists them and never names them.
export interface ProjectDraft {
  name?: string
  compose: string
  env?: string | null
  // Anything the source wants the person to check, shown above the draft.
  notes?: ReactNode
}

export interface ProjectSourceProps {
  nodeId: string
  // Hand a Compose text to the creation form...
  onDraft: (draft: ProjectDraft) => void
  // ...or, for a source that creates the project itself (a Git clone), say it is done.
  onCreated: (name: string) => void
}

// A panel shown on a project's page (the Git source, for one), contributed by a feature.
export interface ProjectPanel {
  id: string
  render: (props: { nodeId: string; project: string }) => ReactNode
}

export interface ProjectSource {
  id: string
  labelKey: string // in the feature's own namespace
  render: (props: ProjectSourceProps) => ReactNode
}

// A button next to the draft that does something with it (keep it as a template...).
export interface ProjectDraftAction {
  id: string
  render: (props: { draft: ProjectDraft }) => ReactNode
}

export interface Feature {
  name: string
  // Backend module that must be enabled for this feature to appear.
  requiresModule?: string
  routes?: RouteObject[]
  // Pages opened without signing in (a public status page): the server decides what they show.
  publicRoutes?: RouteObject[]
  nav?: NavItem[]
  nodeTabs?: NodeTab[]
  objectSections?: ObjectSection[]
  headerWidgets?: HeaderWidget[]
  projectSources?: ProjectSource[]
  projectPanels?: ProjectPanel[]
  projectDraftActions?: ProjectDraftAction[]
  // Live updates: for a change signal (`docker.changed`), the query keys to reload. Signals
  // about one machine already reload every query that mentions that machine.
  signals?: Record<string, string[]>
}

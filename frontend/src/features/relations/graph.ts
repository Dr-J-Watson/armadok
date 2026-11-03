// Pure helpers for the relations map: the server's answer -> what the drawing needs.
// Kept free of React and of the drawing library so it is easy to test.

import type { ElementDefinition } from 'cytoscape'

export interface RelationObject {
  type: string
  id: string
  label: string
}

export interface RelationLink {
  source: { type: string; id: string }
  target: { type: string; id: string }
  kind: string
}

export interface RelationsMap {
  objects: RelationObject[]
  links: RelationLink[]
  unavailable: Record<string, string>
}

// Order of the legend and of the object list. Types a provider adds later are still shown,
// after these.
export const KNOWN_TYPES = [
  'domain',
  'router',
  'middleware',
  'container',
  'project',
  'image',
  'network',
  'volume',
] as const

export const objectKey = (ref: { type: string; id: string }): string => `${ref.type}:${ref.id}`

export function typesOf(objects: RelationObject[]): string[] {
  const present = new Set(objects.map((o) => o.type))
  const known = KNOWN_TYPES.filter((type) => present.has(type))
  const others = [...present].filter((type) => !(KNOWN_TYPES as readonly string[]).includes(type))
  return [...known, ...others.sort()]
}

// Objects and links of the types the user wants to see. A link needs both ends visible.
export function buildElements(map: RelationsMap, visibleTypes: Set<string>): ElementDefinition[] {
  const nodes = map.objects.filter((o) => visibleTypes.has(o.type))
  const shown = new Set(nodes.map(objectKey))
  const edges = map.links.filter(
    (link) => shown.has(objectKey(link.source)) && shown.has(objectKey(link.target)),
  )
  return [
    ...nodes.map((o) => ({
      group: 'nodes' as const,
      data: { id: objectKey(o), label: o.label, type: o.type },
    })),
    ...edges.map((link, index) => ({
      group: 'edges' as const,
      data: {
        id: `e${index}`,
        source: objectKey(link.source),
        target: objectKey(link.target),
        kind: link.kind,
      },
    })),
  ]
}

export function matchesSearch(object: RelationObject, search: string): boolean {
  const needle = search.trim().toLowerCase()
  return !needle || object.label.toLowerCase().includes(needle) || object.id.toLowerCase().includes(needle)
}

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

// Which column a type sits in, from the entry points (domains) to what they end up on (volumes).
// The map reads left to right: who is reached, how, and by what. Unknown types go last.
const COLUMN_RANK: Record<string, number> = {
  domain: 0,
  certificate: 0,
  probe: 0,
  router: 1,
  middleware: 2,
  service: 2,
  container: 3,
  project: 4,
  image: 5,
  network: 5,
  volume: 5,
}
export const rankOf = (type: string): number => COLUMN_RANK[type] ?? 6

export interface MapLayout {
  positions: Record<string, { x: number; y: number }>
  // One entry per column that holds something, left to right.
  columns: { x: number; types: string[] }[]
  top: number
}

export const COLUMN_WIDTH = 190
export const ROW_HEIGHT = 46

// A fixed, readable layout: objects in columns by type, and inside a column ordered by where
// their neighbours stand (a few sweeps of the barycentre rule), which keeps most links short and
// uncrossed. Being computed, not simulated, it is the same at every refresh.
export function layoutMap(nodes: { id: string; type: string }[], edges: { source: string; target: string }[]): MapLayout {
  const ranks = [...new Set(nodes.map((n) => rankOf(n.type)))].sort((a, b) => a - b)
  const byRank = new Map<number, { id: string; type: string }[]>(ranks.map((r) => [r, []]))
  for (const n of [...nodes].sort((a, b) => a.type.localeCompare(b.type) || a.id.localeCompare(b.id))) byRank.get(rankOf(n.type))!.push(n)
  const neighbours = new Map<string, string[]>()
  for (const e of edges) {
    neighbours.set(e.source, [...(neighbours.get(e.source) ?? []), e.target])
    neighbours.set(e.target, [...(neighbours.get(e.target) ?? []), e.source])
  }
  const index = new Map<string, number>()
  const reindex = () => byRank.forEach((list) => list.forEach((n, i) => index.set(n.id, i)))
  reindex()
  const sweep = (order: number[]) => {
    for (const r of order) {
      const list = byRank.get(r)!
      const score = (id: string) => {
        const near = (neighbours.get(id) ?? []).filter((x) => index.has(x))
        return near.length ? near.reduce((sum, x) => sum + index.get(x)!, 0) / near.length : index.get(id)!
      }
      const scored = new Map(list.map((n) => [n.id, score(n.id)]))
      list.sort((a, b) => scored.get(a.id)! - scored.get(b.id)!)
      list.forEach((n, i) => index.set(n.id, i))
    }
  }
  for (let i = 0; i < 4; i += 1) {
    sweep(ranks.slice(1))
    sweep([...ranks].reverse().slice(1))
  }
  const tallest = Math.max(1, ...[...byRank.values()].map((l) => l.length))
  const positions: MapLayout['positions'] = {}
  const columns: MapLayout['columns'] = []
  ranks.forEach((r, c) => {
    const list = byRank.get(r)!
    const offset = ((tallest - list.length) * ROW_HEIGHT) / 2 // short columns are centred
    list.forEach((n, i) => {
      positions[n.id] = { x: c * COLUMN_WIDTH, y: offset + i * ROW_HEIGHT }
    })
    columns.push({ x: c * COLUMN_WIDTH, types: [...new Set(list.map((n) => n.type))] })
  })
  return { positions, columns, top: 0 }
}

// What lies within `depth` links of an object (whichever way the links point), among the objects
// of the visible types. Lets a big map be read one neighbourhood at a time.
export function neighbourhood(map: RelationsMap, key: string, depth: number, visible: Set<string>): RelationsMap {
  const shown = map.objects.filter((o) => visible.has(o.type) || objectKey(o) === key)
  const ok = new Set(shown.map(objectKey))
  const next = new Map<string, Set<string>>()
  for (const l of map.links) {
    const a = objectKey(l.source)
    const b = objectKey(l.target)
    if (!ok.has(a) || !ok.has(b)) continue
    next.set(a, (next.get(a) ?? new Set()).add(b))
    next.set(b, (next.get(b) ?? new Set()).add(a))
  }
  const keep = new Set([key])
  let edge = [key]
  for (let i = 0; i < depth; i += 1) {
    edge = edge.flatMap((k) => [...(next.get(k) ?? [])]).filter((k) => !keep.has(k))
    edge.forEach((k) => keep.add(k))
  }
  return {
    objects: shown.filter((o) => keep.has(objectKey(o))),
    links: map.links.filter((l) => keep.has(objectKey(l.source)) && keep.has(objectKey(l.target))),
    unavailable: map.unavailable,
  }
}

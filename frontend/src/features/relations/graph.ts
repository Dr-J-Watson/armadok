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

// ---------------------------------------------------------------------------------------------
// The overview: objects grouped the way a person thinks of them (a project, its containers, what
// each one uses, and the addresses that reach it), not as a web of dots.

export interface ContainerCard {
  container: RelationObject
  images: RelationObject[]
  networks: RelationObject[]
  volumes: RelationObject[]
}

export interface SiteCard {
  router: RelationObject
  domains: RelationObject[]
  middlewares: RelationObject[]
}

export interface ProjectCard {
  // null for what belongs to no project.
  project: RelationObject | null
  containers: ContainerCard[]
  sites: SiteCard[]
}

export interface Overview {
  projects: ProjectCard[]
  // Objects that no project or container accounts for (unused images, empty networks...), by type.
  loose: { type: string; objects: RelationObject[] }[]
}

// Every object reached from `key` through a link, whichever way the link points.
function adjacency(map: RelationsMap): Map<string, Set<string>> {
  const next = new Map<string, Set<string>>()
  const add = (a: string, b: string) => next.set(a, (next.get(a) ?? new Set()).add(b))
  for (const l of map.links) {
    add(objectKey(l.source), objectKey(l.target))
    add(objectKey(l.target), objectKey(l.source))
  }
  return next
}

export function buildOverview(map: RelationsMap, visible: Set<string>): Overview {
  const byKey = new Map(map.objects.map((o) => [objectKey(o), o]))
  const next = adjacency(map)
  const near = (o: RelationObject, type: string) =>
    [...(next.get(objectKey(o)) ?? [])]
      .map((k) => byKey.get(k))
      .filter((x): x is RelationObject => x !== undefined && x.type === type && visible.has(type))
      .sort((a, b) => a.label.localeCompare(b.label))
  const ofType = (type: string) => map.objects.filter((o) => o.type === type).sort((a, b) => a.label.localeCompare(b.label))

  const used = new Set<string>()
  const mark = (o: RelationObject) => used.add(objectKey(o))
  const containerCard = (c: RelationObject): ContainerCard => {
    mark(c)
    const card = { container: c, images: near(c, 'image'), networks: near(c, 'network'), volumes: near(c, 'volume') }
    ;[...card.images, ...card.networks, ...card.volumes].forEach(mark)
    return card
  }
  // A router reaches containers directly or through a service.
  const containersOf = (router: RelationObject): RelationObject[] => {
    const direct = near(router, 'container')
    const viaService = near(router, 'service').flatMap((s) => near(s, 'container'))
    return [...new Map([...direct, ...viaService].map((c) => [objectKey(c), c])).values()]
  }
  const siteCard = (router: RelationObject): SiteCard => {
    mark(router)
    const card = { router, domains: near(router, 'domain'), middlewares: near(router, 'middleware') }
    ;[...card.domains, ...card.middlewares].forEach(mark)
    near(router, 'service').forEach(mark)
    return card
  }

  const projects: ProjectCard[] = []
  const placedContainers = new Set<string>()
  const placedRouters = new Set<string>()
  if (visible.has('container')) {
    for (const project of ofType('project')) {
      mark(project)
      const members = near(project, 'container')
      members.forEach((c) => placedContainers.add(objectKey(c)))
      const routers = [...new Map(members.flatMap((c) => near(c, 'router')).map((r) => [objectKey(r), r])).values()]
      routers.forEach((r) => placedRouters.add(objectKey(r)))
      projects.push({ project, containers: members.map(containerCard), sites: visible.has('router') ? routers.map(siteCard) : [] })
    }
    const alone = ofType('container').filter((c) => !placedContainers.has(objectKey(c)))
    if (alone.length > 0) {
      const routers = [...new Map(alone.flatMap((c) => near(c, 'router')).map((r) => [objectKey(r), r])).values()].filter((r) => !placedRouters.has(objectKey(r)))
      routers.forEach((r) => placedRouters.add(objectKey(r)))
      projects.push({ project: null, containers: alone.map(containerCard), sites: visible.has('router') ? routers.map(siteCard) : [] })
    }
  }
  // Sites whose container is not shown (a service on another machine, a container filtered out).
  if (visible.has('router')) {
    const rest = ofType('router').filter((r) => !placedRouters.has(objectKey(r)) && containersOf(r).length === 0)
    if (rest.length > 0) projects.push({ project: null, containers: [], sites: rest.map(siteCard) })
  }

  const loose = typesOf(map.objects)
    .map((type) => ({ type, objects: ofType(type).filter((o) => visible.has(type) && !used.has(objectKey(o))) }))
    .filter((group) => group.objects.length > 0)
  return { projects, loose }
}

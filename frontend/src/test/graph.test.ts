import { buildElements, buildOverview, layoutMap, matchesSearch, objectKey, typesOf } from '../features/relations/graph'
import type { RelationsMap } from '../features/relations/graph'

const map: RelationsMap = {
  objects: [
    { type: 'container', id: 'c1', label: 'web' },
    { type: 'network', id: 'n1', label: 'demo_default' },
    { type: 'router', id: 'web', label: 'web' },
    { type: 'custom', id: 'x', label: 'extra' },
  ],
  links: [
    { source: { type: 'container', id: 'c1' }, target: { type: 'network', id: 'n1' }, kind: 'attached_to' },
    { source: { type: 'router', id: 'web' }, target: { type: 'container', id: 'c1' }, kind: 'routes_to' },
  ],
  unavailable: {},
}

describe('relations graph helpers', () => {
  it('lists types in a stable order, unknown ones last', () => {
    expect(typesOf(map.objects)).toEqual(['router', 'container', 'network', 'custom'])
  })

  it('keeps an object of two types with the same id apart (web router vs web container)', () => {
    const keys = map.objects.map(objectKey)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('hides links whose end is hidden, so no edge points at nothing', () => {
    const all = buildElements(map, new Set(['router', 'container', 'network', 'custom']))
    expect(all.filter((e) => e.group === 'edges')).toHaveLength(2)
    const withoutNetworks = buildElements(map, new Set(['router', 'container', 'custom']))
    const edges = withoutNetworks.filter((e) => e.group === 'edges')
    expect(edges).toHaveLength(1)
    expect(edges[0].data.kind).toBe('routes_to')
    expect(withoutNetworks.some((e) => e.data.id === 'network:n1')).toBe(false)
  })

  it('searches labels and ids without case sensitivity', () => {
    expect(matchesSearch(map.objects[1], 'DEMO')).toBe(true)
    expect(matchesSearch(map.objects[1], 'zzz')).toBe(false)
    expect(matchesSearch(map.objects[1], '  ')).toBe(true)
  })
})

describe('relations map layout', () => {
  it('puts types in columns from domains to volumes and keeps linked objects level', () => {
    const nodes = [
      { id: 'volume:v', type: 'volume' },
      { id: 'domain:a.example.com', type: 'domain' },
      { id: 'container:c', type: 'container' },
      { id: 'router:r', type: 'router' },
      { id: 'container:d', type: 'container' },
    ]
    const edges = [
      { source: 'domain:a.example.com', target: 'router:r' },
      { source: 'router:r', target: 'container:c' },
      { source: 'container:c', target: 'volume:v' },
    ]
    const { positions, columns } = layoutMap(nodes, edges)
    expect(columns.map((c) => c.types)).toEqual([['domain'], ['router'], ['container'], ['volume']])
    const x = (id: string) => positions[id].x
    expect(x('domain:a.example.com')).toBeLessThan(x('router:r'))
    expect(x('router:r')).toBeLessThan(x('container:c'))
    expect(x('container:c')).toBeLessThan(x('volume:v'))
    // The container that is linked sits next to the router, the loose one after it.
    expect(positions['container:c'].y).toBeLessThan(positions['container:d'].y)
  })

  it('gives the same layout for the same data', () => {
    const nodes = [{ id: 'a', type: 'container' }, { id: 'b', type: 'container' }]
    expect(layoutMap(nodes, [])).toEqual(layoutMap([...nodes].reverse(), []))
  })
})

describe('relations overview', () => {
  const m: RelationsMap = {
    objects: [
      { type: 'project', id: 'app', label: 'app' },
      { type: 'container', id: 'c1', label: 'app-web-1' },
      { type: 'image', id: 'i1', label: 'nginx' },
      { type: 'image', id: 'i2', label: 'unused' },
      { type: 'router', id: 'r', label: 'app' },
      { type: 'domain', id: 'd', label: 'app.example.com' },
      { type: 'container', id: 'c2', label: 'traefik' },
    ],
    links: [
      { source: { type: 'container', id: 'c1' }, target: { type: 'project', id: 'app' }, kind: 'part_of' },
      { source: { type: 'container', id: 'c1' }, target: { type: 'image', id: 'i1' }, kind: 'runs' },
      { source: { type: 'router', id: 'r' }, target: { type: 'container', id: 'c1' }, kind: 'routes_to' },
      { source: { type: 'domain', id: 'd' }, target: { type: 'router', id: 'r' }, kind: 'serves' },
    ],
    unavailable: {},
  }
  const all = new Set(typesOf(m.objects))

  it('groups a project with its containers, their image and the addresses that reach them', () => {
    const { projects, loose } = buildOverview(m, all)
    expect(projects[0].project?.id).toBe('app')
    expect(projects[0].containers[0].images.map((i) => i.label)).toEqual(['nginx'])
    expect(projects[0].sites[0].domains.map((d) => d.label)).toEqual(['app.example.com'])
    // A container outside any project gets a card of its own; an unused image is listed apart.
    expect(projects[1].project).toBeNull()
    expect(loose).toEqual([{ type: 'image', objects: [m.objects[3]] }])
  })
})

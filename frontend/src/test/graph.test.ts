import { buildElements, layoutGroups, layoutMap, matchesSearch, neighbourhood, objectKey, typesOf } from '../features/relations/graph'
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

describe('relations neighbourhood', () => {
  it('does not walk on through an object that touches everything', () => {
    const objects = [{ type: 'container', id: 'a', label: 'a' }, { type: 'network', id: 'hub', label: 'hub' }, ...Array.from({ length: 15 }, (_, i) => ({ type: 'container', id: `x${i}`, label: `x${i}` }))]
    const links = objects.filter((o) => o.id !== 'hub').map((o) => ({ source: { type: 'container', id: o.id }, target: { type: 'network', id: 'hub' }, kind: 'attached_to' }))
    const sub = neighbourhood({ objects, links, unavailable: {} }, 'container:a', 2, new Set(['container', 'network']))
    expect(sub.objects.map((o) => o.id).sort()).toEqual(['a', 'hub'])
  })

  const m: RelationsMap = {
    objects: ['a', 'b', 'c', 'd'].map((id) => ({ type: id === 'd' ? 'image' : 'container', id, label: id })),
    links: [
      { source: { type: 'container', id: 'a' }, target: { type: 'container', id: 'b' }, kind: 'uses' },
      { source: { type: 'container', id: 'c' }, target: { type: 'container', id: 'b' }, kind: 'uses' },
      { source: { type: 'container', id: 'c' }, target: { type: 'image', id: 'd' }, kind: 'runs' },
    ],
    unavailable: {},
  }
  const all = new Set(['container', 'image'])

  it('keeps what is within the asked number of links, in either direction', () => {
    expect(neighbourhood(m, 'container:a', 1, all).objects.map((o) => o.id)).toEqual(['a', 'b'])
    expect(neighbourhood(m, 'container:a', 2, all).objects.map((o) => o.id)).toEqual(['a', 'b', 'c'])
    expect(neighbourhood(m, 'container:a', 3, all).links).toHaveLength(3)
  })

  it('does not walk through a hidden type', () => {
    expect(neighbourhood(m, 'container:a', 3, new Set(['container'])).objects.map((o) => o.id)).toEqual(['a', 'b', 'c'])
  })
})

describe('relations group layout', () => {
  it('draws separate groups apart and packs loose objects after the big ones', () => {
    const nodes = ['a', 'b', 'c', 'x', 'y'].map((id) => ({ id, type: 'container' }))
    const edges = [
      { source: 'a', target: 'b' },
      { source: 'b', target: 'c' },
    ]
    const where = layoutGroups(nodes, edges)
    expect(Object.keys(where).sort()).toEqual(['a', 'b', 'c', 'x', 'y'])
    // The linked group comes first (left), and x and y do not sit on top of it or of each other.
    expect(where.a.x).toBe(0)
    expect(where.x.x).toBeGreaterThan(where.c.x)
    expect(where.x).not.toEqual(where.y)
  })
})

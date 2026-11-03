// Rights as the server describes them (`GET /rights`) and helpers to pick a consistent set.

export interface RightInfo {
  id: string
  depends_on: string[]
  sensitive: boolean
  node_owner: boolean
}

// A right is unusable without its dependencies, so choosing one chooses them too...
export function withDependencies(chosen: Set<string>, catalogue: RightInfo[], right: string): Set<string> {
  const byId = new Map(catalogue.map((r) => [r.id, r]))
  const next = new Set(chosen)
  const add = (id: string) => {
    if (next.has(id)) return
    next.add(id)
    byId.get(id)?.depends_on.forEach(add)
  }
  add(right)
  return next
}

// ...and dropping one drops everything that depends on it.
export function withoutDependents(chosen: Set<string>, catalogue: RightInfo[], right: string): Set<string> {
  const next = new Set(chosen)
  next.delete(right)
  let changed = true
  while (changed) {
    changed = false
    for (const info of catalogue)
      if (next.has(info.id) && info.depends_on.some((dep) => !next.has(dep))) {
        next.delete(info.id)
        changed = true
      }
  }
  return next
}

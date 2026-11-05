import { Boxes, Network, Server } from 'lucide-react'

// One icon per kind of machine: Docker (containers), network (Traefik and DNS), or both.
export function NodeIcon({ type, size = 20 }: { type: string; size?: number }) {
  const Icon = type === 'docker' ? Boxes : type === 'network' ? Network : Server
  return <Icon size={size} strokeWidth={1.8} aria-hidden="true" />
}

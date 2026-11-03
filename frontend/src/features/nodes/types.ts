export type NodeType = 'network' | 'docker' | 'full'

export interface Node {
  id: string
  name: string
  type: NodeType
  description: string
  owner: string
  created_at: string
}

import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { Link, useLocation, useMatch, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Settings } from 'lucide-react'
import { NavIcon } from '../components/NavIcon'
import { api } from '../core/api'
import { readPref, writePref } from '../core/storage'
import { modesOf, useNodeTabs } from '../features/nodes/useNodeTabs'
import type { Mode } from '../features/nodes/useNodeTabs'
import type { Node } from '../features/nodes/types'

// Which node the person is working on: the one in the address when they are on its page,
// else the one they used last (a preference of this browser), else the first they can see.
function useActiveNode(nodes: Node[] | undefined): Node | undefined {
  const match = useMatch('/nodes/:nodeId/*')
  const here = match?.params.nodeId
  const remembered = readPref('activeNode')
  const active = nodes?.find((n) => n.id === here) ?? nodes?.find((n) => n.id === remembered) ?? nodes?.[0]
  useEffect(() => {
    if (active) writePref('activeNode', active.id)
  }, [active])
  return active
}

// The top of the side menu: pick the node, then see only what that node can do. A node with
// both kinds of worker offers a switch between Docker and Network; a node with one shows its
// name as a plain label. Entries come from the features' node tabs, so a new feature appears
// here by declaring where its tab lives.
export function NodeSwitcher({ onGo }: { onGo: () => void }) {
  const { t } = useTranslation(['nodes', 'common'])
  const navigate = useNavigate()
  const location = useLocation()
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const node = useActiveNode(nodes.data)
  const tabs = useNodeTabs(node)
  if (!node || !nodes.data) return null

  const currentTab = new URLSearchParams(location.search).get('tab')
  const onNodePage = location.pathname === `/nodes/${node.id}`
  const tabArea = tabs.find((x) => x.id === currentTab)?.area
  const modes = modesOf(node).filter((m) => tabs.some((x) => x.area === m))
  const remembered = readPref(`mode.${node.id}`)
  const mode: Mode | undefined =
    (onNodePage && (tabArea === 'docker' || tabArea === 'network') ? tabArea : undefined) ??
    modes.find((m) => m === remembered) ??
    modes[0]

  const go = (nodeId: string, tab?: string) => navigate(tab ? `/nodes/${nodeId}?tab=${tab}` : `/nodes/${nodeId}`)
  const pickMode = (next: Mode) => {
    writePref(`mode.${node.id}`, next)
    const first = tabs.find((x) => x.area === next)
    if (first) go(node.id, first.id)
  }
  const link = (id: string | null, label: string, icon: ReactNode) => {
    const active = onNodePage && currentTab === id
    return (
      <Link key={id ?? 'settings'} to={id ? `/nodes/${node.id}?tab=${id}` : `/nodes/${node.id}`} onClick={onGo} className="nav-link" aria-current={active ? 'page' : undefined}>
        {icon}
        <span>{label}</span>
      </Link>
    )
  }
  // The settings page holds the settings tabs; it is "current" for any of them.
  const inSettings = onNodePage && (currentTab === null || tabs.find((x) => x.id === currentTab)?.area === undefined)
  const modeTabs = tabs.filter((x) => x.area === mode)
  const nodeTabs = tabs.filter((x) => x.area === 'node')

  return (
    <div className="node-switcher">
      <label className="node-select">
        <span className="visually-hidden">{t('nodes:switch.label')}</span>
        <select value={node.id} onChange={(e) => go(e.target.value, onNodePage ? (currentTab ?? undefined) : undefined)}>
          {nodes.data.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name}
            </option>
          ))}
        </select>
      </label>
      {modes.length > 1 ? (
        <div className="mode-switch" role="group" aria-label={t('nodes:switch.mode')}>
          {modes.map((m) => (
            <button key={m} type="button" aria-pressed={m === mode} className={m === mode ? 'primary' : undefined} onClick={() => pickMode(m)}>
              {t(`nodes:area.${m}`)}
            </button>
          ))}
        </div>
      ) : (
        modes[0] && <p className="mode-label">{t(`nodes:area.${modes[0]}`)}</p>
      )}
      <nav aria-label={t('nodes:switch.menu', { name: node.name })}>
        {modeTabs.map((x) => link(x.id, t(x.labelKey), <NavIcon name={x.icon} />))}
        {nodeTabs.map((x) => link(x.id, t(x.labelKey), <NavIcon name={x.icon} />))}
        <Link to={`/nodes/${node.id}`} onClick={onGo} className="nav-link" aria-current={inSettings ? 'page' : undefined}>
          <Settings size={18} strokeWidth={1.8} aria-hidden="true" className="nav-icon" />
          <span>{t('nodes:settings')}</span>
        </Link>
      </nav>
    </div>
  )
}

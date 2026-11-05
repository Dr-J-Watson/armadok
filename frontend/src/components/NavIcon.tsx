import {
  Bell,
  Bot,
  BookOpen,
  Bug,
  Circle,
  Gift,
  Globe,
  HardDrive,
  KeyRound,
  NotebookPen,
  Plug,
  Rocket,
  ScrollText,
  Server,
  ShieldCheck,
  User,
  Users,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

// Icons a feature can ask for by name in its menu entry, so the shell never imports them one by
// one for each feature. An unknown name gets a plain dot, never a broken menu.
const ICONS: Record<string, LucideIcon> = {
  rocket: Rocket,
  server: Server,
  globe: Globe,
  users: Users,
  key: KeyRound,
  plug: Plug,
  bot: Bot,
  audit: ScrollText,
  bug: Bug,
  backup: HardDrive,
  shield: ShieldCheck,
  notes: NotebookPen,
  gift: Gift,
  help: BookOpen,
  user: User,
  bell: Bell,
}

export function NavIcon({ name, size = 18 }: { name?: string; size?: number }) {
  const Icon = (name && ICONS[name]) || Circle
  return <Icon size={size} strokeWidth={1.8} aria-hidden="true" className="nav-icon" />
}

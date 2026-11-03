import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'

interface Session {
  id: string
  node_id: string
  node_name: string
  expires_at: string
}

function left(expires: string, now: number): string {
  const seconds = Math.max(0, Math.round((new Date(expires).getTime() - now) / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

// Shown on every page while the person holds an emergency session: it is a serious thing to be
// in, so it is never out of sight, with the time left and a way out.
export function EmergencyBanner() {
  const { t } = useTranslation(['emergency', 'errors'])
  const queryClient = useQueryClient()
  const sessions = useQuery({
    queryKey: ['emergency-active'],
    queryFn: () => api.get<Session[]>('/emergency/active'),
    refetchInterval: 60000,
    retry: false,
  })
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const end = useMutation({
    mutationFn: (nodeId: string) => api.del(`/admin/nodes/${nodeId}/emergency`),
    onSuccess: () => queryClient.invalidateQueries(),
  })
  // A session that ran out is dropped from the screen at once, before the server is asked.
  const open = (sessions.data ?? []).filter((s) => new Date(s.expires_at).getTime() > now)
  if (open.length === 0) return null
  return (
    <div role="alert" className="emergency-banner">
      {open.map((s) => (
        <span key={s.id}>
          <strong>{t('emergency:banner', { node: s.node_name, time: left(s.expires_at, now) })}</strong>{' '}
          <Link to={`/nodes/${s.node_id}`}>{t('emergency:open')}</Link>{' '}
          <button type="button" onClick={() => end.mutate(s.node_id)}>
            {t('emergency:end')}
          </button>
        </span>
      ))}
    </div>
  )
}

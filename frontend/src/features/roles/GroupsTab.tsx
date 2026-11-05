import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { useCan } from '../../core/auth'
import type { Group } from './types'
import { Problem } from '../../components/Problem'

interface User {
  id: string
  username: string
}

// Groups of people: a role given to a group reaches every member.
export function GroupsTab() {
  const { t } = useTranslation(['roles', 'errors'])
  const queryClient = useQueryClient()
  const canManage = useCan('grants.manage')
  const groups = useQuery({ queryKey: ['groups'], queryFn: () => api.get<Group[]>('/groups') })
  const users = useQuery({ queryKey: ['users'], queryFn: () => api.get<User[]>('/users') })
  const [name, setName] = useState('')
  const [adding, setAdding] = useState<Record<string, string>>({})
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['groups'] })
  const create = useMutation({
    mutationFn: () => api.post('/groups', { name }),
    onSuccess: () => {
      setName('')
      return refresh()
    },
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`/groups/${id}`), onSuccess: refresh })
  const join = useMutation({
    mutationFn: (v: { group: string; user: string }) => api.post(`/groups/${v.group}/members`, { user_id: v.user }),
    onSuccess: refresh,
  })
  const leave = useMutation({
    mutationFn: (v: { group: string; user: string }) => api.del(`/groups/${v.group}/members/${v.user}`),
    onSuccess: refresh,
  })

  const nameOf = (id: string) => users.data?.find((u) => u.id === id)?.username ?? id
  if (groups.isPending) return <p>{t('loading')}</p>
  if (groups.isError)
    return (
      <Problem error={groups.error} />
    )
  const failed = create.error ?? remove.error ?? join.error ?? leave.error

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  return (
    <>
      {failed != null && (
        <Problem error={failed} />
      )}
      {canManage && (
        <form onSubmit={submit} aria-label={t('roles:groups.new')}>
          <label>
            {t('roles:groups.name')}
            <input value={name} required maxLength={60} onChange={(e) => setName(e.target.value)} />
          </label>
          <button type="submit" disabled={create.isPending}>
            {t('roles:groups.create')}
          </button>
        </form>
      )}
      {groups.data.length === 0 && <p className="empty">{t('roles:groups.none')}</p>}
      <ul className="links">
        {groups.data.map((g) => (
          <li key={g.id}>
            <strong>{g.name}</strong>
            <ul className="links">
              {g.members.map((m) => (
                <li key={m}>
                  {nameOf(m)}{' '}
                  {canManage && (
                    <button type="button" onClick={() => leave.mutate({ group: g.id, user: m })}>
                      {t('roles:groups.remove')}
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {canManage && (
              <div className="actions">
                <select
                  aria-label={t('roles:groups.addMember')}
                  value={adding[g.id] ?? ''}
                  onChange={(e) => setAdding({ ...adding, [g.id]: e.target.value })}
                >
                  <option value="">{t('roles:groups.pick')}</option>
                  {(users.data ?? [])
                    .filter((u) => !g.members.includes(u.id))
                    .map((u) => (
                      <option key={u.id} value={u.id}>
                        {u.username}
                      </option>
                    ))}
                </select>
                <button
                  type="button"
                  disabled={!adding[g.id]}
                  onClick={() => join.mutate({ group: g.id, user: adding[g.id] })}
                >
                  {t('roles:groups.add')}
                </button>
                <button type="button" onClick={() => remove.mutate(g.id)}>
                  {t('roles:groups.delete')}
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}

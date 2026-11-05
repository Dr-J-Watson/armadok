import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { useCan } from '../../core/auth'
import type { Node } from '../nodes/types'
import { roleLabel } from './RolesTab'
import type { Assignment, Group, Role } from './types'
import { Problem } from '../../components/Problem'

interface User {
  id: string
  username: string
}

// Giving a role to a person or a group, on one node or everywhere, until a date if wanted.
export function AssignmentsTab() {
  const { t } = useTranslation(['roles', 'users', 'errors'])
  const queryClient = useQueryClient()
  const canManage = useCan('grants.manage')
  const list = useQuery({ queryKey: ['assignments'], queryFn: () => api.get<Assignment[]>('/assignments') })
  const roles = useQuery({ queryKey: ['roles'], queryFn: () => api.get<Role[]>('/roles') })
  const users = useQuery({ queryKey: ['users'], queryFn: () => api.get<User[]>('/users') })
  const groups = useQuery({ queryKey: ['groups'], queryFn: () => api.get<Group[]>('/groups') })
  const nodes = useQuery({ queryKey: ['nodes'], queryFn: () => api.get<Node[]>('/nodes') })
  const [form, setForm] = useState({ type: 'user', subject: '', role: '', scope: '*', expires: '' })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['assignments'] })
  const add = useMutation({
    mutationFn: () =>
      api.post('/assignments', {
        subject_type: form.type,
        subject_id: form.subject,
        role_id: form.role,
        scope: form.scope,
        expires_at: form.expires ? new Date(form.expires).toISOString() : null,
      }),
    onSuccess: refresh,
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`/assignments/${id}`), onSuccess: refresh })

  if (list.isPending) return <p>{t('loading')}</p>
  if (list.isError)
    return (
      <Problem error={list.error} />
    )
  const subjects = form.type === 'user' ? (users.data ?? []).map((u) => ({ id: u.id, name: u.username })) : (groups.data ?? [])
  const scopeName = (scope: string) =>
    scope.startsWith('node:') ? (nodes.data?.find((n) => n.id === scope.slice(5))?.name ?? scope) : t('users:scope.everywhere')
  const failed = add.error ?? remove.error

  function submit(event: FormEvent) {
    event.preventDefault()
    add.mutate()
  }

  return (
    <>
      {failed != null && (
        <Problem error={failed} />
      )}
      {canManage && (
        <form onSubmit={submit} aria-label={t('roles:assign.title')}>
          <label>
            {t('roles:assign.subjectType')}
            <select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value, subject: '' })}>
              <option value="user">{t('roles:assign.user')}</option>
              <option value="group">{t('roles:assign.group')}</option>
            </select>
          </label>
          <label>
            {t('roles:assign.subject')}
            <select value={form.subject} required onChange={(e) => setForm({ ...form, subject: e.target.value })}>
              <option value="" />
              {subjects.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('roles:assign.role')}
            <select value={form.role} required onChange={(e) => setForm({ ...form, role: e.target.value })}>
              <option value="" />
              {(roles.data ?? []).map((r) => (
                <option key={r.id} value={r.id}>
                  {roleLabel(t, r)}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('roles:assign.scope')}
            <select value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}>
              <option value="*">{t('users:scope.everywhere')}</option>
              {(nodes.data ?? []).map((n) => (
                <option key={n.id} value={`node:${n.id}`}>
                  {n.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('roles:assign.expires')}
            <input type="datetime-local" value={form.expires} onChange={(e) => setForm({ ...form, expires: e.target.value })} />
          </label>
          <button type="submit" disabled={add.isPending}>
            {t('roles:assign.give')}
          </button>
        </form>
      )}
      {list.data.length === 0 && <p className="empty">{t('roles:assign.none')}</p>}
      <table className="table">
        <tbody>
          {list.data.map((a) => (
            <tr key={a.id}>
              <td>
                {a.subject_name ?? a.subject_id} <small>({t(`roles:assign.${a.subject_type}`)})</small>
              </td>
              <td>{roleLabel(t, { name: a.role_name, builtin: !!a.role_builtin })}</td>
              <td>{scopeName(a.scope)}</td>
              <td>{a.expires_at ? new Date(a.expires_at).toLocaleString() : t('roles:assign.never')}</td>
              <td>
                {canManage && (
                  <button type="button" onClick={() => remove.mutate(a.id)}>
                    {t('roles:assign.take')}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

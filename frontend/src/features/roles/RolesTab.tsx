import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { useCan } from '../../core/auth'
import type { RightInfo } from '../account/rights'
import type { Role } from './types'
import { Problem } from '../../components/Problem'

// Built-in roles carry a translated name; custom roles show the name they were given.
export function roleLabel(t: (key: string) => string, role: { name: string; builtin: boolean }) {
  return role.builtin ? t(`roles:builtin.${role.name}`) : role.name
}

// Roles: what each includes, copying a built-in one, and editing the custom ones.
export function RolesTab() {
  const { t } = useTranslation(['roles', 'errors'])
  const queryClient = useQueryClient()
  const canManage = useCan('grants.manage')
  const roles = useQuery({ queryKey: ['roles'], queryFn: () => api.get<Role[]>('/roles') })
  const catalogue = useQuery({ queryKey: ['rights'], queryFn: () => api.get<RightInfo[]>('/rights') })
  const [editing, setEditing] = useState<{ id: string | null; name: string; rights: string[] } | null>(null)
  const [copyOf, setCopyOf] = useState<{ id: string; name: string } | null>(null)

  const refresh = async () => {
    setEditing(null)
    setCopyOf(null)
    await queryClient.invalidateQueries({ queryKey: ['roles'] })
  }
  const save = useMutation({
    mutationFn: (r: { id: string | null; name: string; rights: string[] }) =>
      r.id ? api.put(`/roles/${r.id}`, { name: r.name, rights: r.rights }) : api.post('/roles', { name: r.name, rights: r.rights }),
    onSuccess: refresh,
  })
  const duplicate = useMutation({
    mutationFn: (c: { id: string; name: string }) => api.post(`/roles/${c.id}/duplicate`, { name: c.name }),
    onSuccess: refresh,
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`/roles/${id}`), onSuccess: refresh })

  if (roles.isPending) return <p>{t('loading')}</p>
  if (roles.isError)
    return (
      <Problem error={roles.error} />
    )
  const sensitive = new Set((catalogue.data ?? []).filter((r) => r.sensitive).map((r) => r.id))
  const assignable = (catalogue.data ?? [])
    .map((r) => r.id)
    .filter((id) => roles.data.some((role) => role.rights.includes(id)) || id.startsWith('nodes.'))
  const failed = save.error ?? duplicate.error ?? remove.error

  function submit(event: FormEvent) {
    event.preventDefault()
    if (editing) save.mutate(editing)
  }

  return (
    <>
      <p>{t('roles:explain')}</p>
      {failed != null && (
        <Problem error={failed} />
      )}
      {canManage && !editing && (
        <div className="actions">
          <button type="button" onClick={() => setEditing({ id: null, name: '', rights: [] })}>
            {t('roles:new')}
          </button>
        </div>
      )}
      {editing && (
        <form onSubmit={submit} aria-label={t('roles:edit')}>
          <label>
            {t('roles:name')}
            <input value={editing.name} required maxLength={60} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
          </label>
          <fieldset>
            <legend>{t('roles:rights')}</legend>
            {assignable.map((id) => (
              <label key={id}>
                <input
                  type="checkbox"
                  checked={editing.rights.includes(id)}
                  onChange={(e) =>
                    setEditing({
                      ...editing,
                      rights: e.target.checked ? [...editing.rights, id] : editing.rights.filter((x) => x !== id),
                    })
                  }
                />
                <code>{id}</code>
                {sensitive.has(id) && <span className="badge off">{t('roles:sensitive')}</span>}
              </label>
            ))}
          </fieldset>
          <div className="actions">
            <button type="submit" className="primary" disabled={save.isPending}>
              {t('roles:save')}
            </button>
            <button type="button" onClick={() => setEditing(null)}>
              {t('roles:cancel')}
            </button>
          </div>
        </form>
      )}
      <ul className="links">
        {roles.data.map((role) => (
          <li key={role.id}>
            <strong>{roleLabel(t, role)}</strong>{' '}
            <small>{role.builtin ? t('roles:builtinNote') : t('roles:custom')}</small>
            <details>
              <summary>{t('roles:includes', { count: role.rights.length })}</summary>
              <p>
                {role.rights.map((r) => (
                  <code key={r}>{r} </code>
                ))}
              </p>
            </details>
            {canManage && (
              <div className="actions">
                <button type="button" onClick={() => setCopyOf({ id: role.id, name: `${roleLabel(t, role)} (2)` })}>
                  {t('roles:duplicate')}
                </button>
                {!role.builtin && (
                  <>
                    <button type="button" onClick={() => setEditing({ id: role.id, name: role.name, rights: role.rights })}>
                      {t('roles:edit')}
                    </button>
                    <button type="button" onClick={() => remove.mutate(role.id)}>
                      {t('roles:delete')}
                    </button>
                  </>
                )}
              </div>
            )}
            {copyOf?.id === role.id && (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  duplicate.mutate(copyOf)
                }}
              >
                <label>
                  {t('roles:copyName')}
                  <input value={copyOf.name} required maxLength={60} onChange={(e) => setCopyOf({ ...copyOf, name: e.target.value })} />
                </label>
                <button type="submit">{t('roles:duplicate')}</button>
              </form>
            )}
          </li>
        ))}
      </ul>
    </>
  )
}

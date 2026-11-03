import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'
import { roleLabel } from '../roles/RolesTab'

interface Member {
  assignment_id: string
  subject_type: 'user' | 'group'
  subject_name: string | null
  role_id: string
  role_name: string
  role_builtin: boolean
  expires_at: string | null
}

interface Person {
  id: string
  username: string
}

// A box that suggests people by name as you type (for those who may not list all accounts).
function PersonPicker({ value, onPick }: { value: Person | null; onPick: (p: Person | null) => void }) {
  const { t } = useTranslation(['members'])
  const [text, setText] = useState('')
  const found = useQuery({
    queryKey: ['directory', text],
    enabled: text.length > 0 && /^[A-Za-z0-9_.@-]+$/.test(text),
    queryFn: () => api.get<Person[]>(`/directory/users?q=${encodeURIComponent(text)}`),
    retry: false,
  })
  return (
    <>
      <label>
        {t('members:person')}
        <input
          list="people"
          value={value ? value.username : text}
          onChange={(e) => {
            setText(e.target.value)
            onPick(found.data?.find((p) => p.username === e.target.value) ?? null)
          }}
        />
      </label>
      <datalist id="people">
        {found.data?.map((p) => (
          <option key={p.id} value={p.username} />
        ))}
      </datalist>
    </>
  )
}

// Who works on a node: invite, change the role of, or remove people; and hand the node over.
export function MembersTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['members', 'roles', 'errors'])
  const queryClient = useQueryClient()
  const base = `/nodes/${nodeId}`
  const list = useQuery({
    queryKey: ['members', nodeId],
    queryFn: () => api.get<{ owner: { id: string; name: string }; members: Member[] }>(`${base}/members`),
    retry: false,
  })
  const roles = useQuery({
    queryKey: ['member-roles', nodeId],
    queryFn: () => api.get<{ id: string; name: string; builtin: boolean }[]>(`${base}/members/roles`),
    enabled: list.isSuccess,
  })
  const [person, setPerson] = useState<Person | null>(null)
  const [role, setRole] = useState('')
  const [target, setTarget] = useState<Person | null>(null)
  const [confirm, setConfirm] = useState('')
  const [code, setCode] = useState('')
  const node = useQuery({ queryKey: ['nodes', nodeId], queryFn: () => api.get<{ name: string }>(base) })

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['members', nodeId] })
  }
  const add = useMutation({
    mutationFn: () => api.post(`${base}/members`, { user_id: person?.id, role_id: role }),
    onSuccess: async () => {
      setPerson(null)
      await refresh()
    },
  })
  const change = useMutation({
    mutationFn: (v: { id: string; role: string }) => api.patch(`${base}/members/${v.id}`, { role_id: v.role }),
    onSuccess: refresh,
  })
  const remove = useMutation({ mutationFn: (id: string) => api.del(`${base}/members/${id}`), onSuccess: refresh })
  const transfer = useMutation({
    mutationFn: () => api.post(`${base}/transfer`, { user_id: target?.id, confirm_name: confirm, code }),
    onSuccess: async () => {
      setTarget(null)
      setConfirm('')
      setCode('')
      await queryClient.invalidateQueries()
    },
  })

  if (list.isPending) return <p>{t('loading')}</p>
  if (list.isError) return <p className="hint">{t('members:notAllowed')}</p>
  const failed = add.error ?? change.error ?? remove.error ?? transfer.error
  const roleOptions = roles.data ?? []
  function submit(event: FormEvent) {
    event.preventDefault()
    add.mutate()
  }

  return (
    <>
      <p>
        {t('members:owner')}: <strong>{list.data.owner.name}</strong>
      </p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      <form onSubmit={submit} aria-label={t('members:add')}>
        <PersonPicker value={person} onPick={setPerson} />
        <label>
          {t('members:role')}
          <select value={role} required onChange={(e) => setRole(e.target.value)}>
            <option value="" />
            {roleOptions.map((r) => (
              <option key={r.id} value={r.id}>
                {roleLabel(t, r)}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={!person || add.isPending}>
          {t('members:invite')}
        </button>
      </form>
      {list.data.members.length === 0 && <p className="empty">{t('members:none')}</p>}
      <ul className="links">
        {list.data.members.map((m) => (
          <li key={m.assignment_id}>
            <strong>{m.subject_name ?? '?'}</strong>{' '}
            <select
              aria-label={t('members:changeRole', { name: m.subject_name })}
              value={m.role_id}
              onChange={(e) => change.mutate({ id: m.assignment_id, role: e.target.value })}
            >
              {[...roleOptions, ...(roleOptions.some((r) => r.id === m.role_id) ? [] : [{ id: m.role_id, name: m.role_name, builtin: m.role_builtin }])].map((r) => (
                <option key={r.id} value={r.id}>
                  {roleLabel(t, r)}
                </option>
              ))}
            </select>{' '}
            {m.expires_at && <small>{t('members:until', { date: new Date(m.expires_at).toLocaleString() })}</small>}{' '}
            <button type="button" onClick={() => remove.mutate(m.assignment_id)}>
              {t('members:remove')}
            </button>
          </li>
        ))}
      </ul>
      <section>
        <h3>{t('members:transfer.title')}</h3>
        <p>{t('members:transfer.explain')}</p>
        <PersonPicker value={target} onPick={setTarget} />
        <label>
          {t('members:transfer.confirm', { name: node.data?.name ?? '' })}
          <input value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </label>
        <label>
          {t('members:transfer.code')}
          <input value={code} autoComplete="one-time-code" onChange={(e) => setCode(e.target.value)} />
        </label>
        <button type="button" disabled={!target || !code || confirm !== node.data?.name || transfer.isPending} onClick={() => transfer.mutate()}>
          {t('members:transfer.go')}
        </button>
      </section>
    </>
  )
}

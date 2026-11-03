import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface AdminNode {
  id: string
  name: string
  type: string
  owner: string
  local: boolean
  state: 'active' | 'suspended' | 'deleted'
  suspended_reason: string | null
  restorable_until: string | null
}

// What an administrator may do with machines that are not theirs: see that they exist, suspend,
// resume or restore them. Never their content. Each action asks for the name and a fresh code.
export function NodeAdminPage() {
  const { t } = useTranslation(['nodeadmin', 'errors'])
  const queryClient = useQueryClient()
  const nodes = useQuery({ queryKey: ['admin-nodes'], queryFn: () => api.get<AdminNode[]>('/admin/nodes') })
  const [open, setOpen] = useState<{ id: string; action: 'suspend' | 'resume' | 'restore' | 'emergency' } | null>(null)
  const [form, setForm] = useState({ name: '', code: '', reason: '' })
  const impact = useQuery({
    queryKey: ['admin-impact', open?.id],
    enabled: open?.action === 'suspend',
    queryFn: () => api.get<{ members: number; worker_connected: boolean }>(`/admin/nodes/${open?.id}/impact`),
  })
  const run = useMutation({
    mutationFn: () =>
      api.post(`/admin/nodes/${open?.id}/${open?.action}`, {
        ...(open?.action === 'emergency' ? {} : { confirm_name: form.name }),
        code: form.code,
        ...(open?.action === 'suspend' || open?.action === 'emergency' ? { reason: form.reason } : {}),
      }),
    onSuccess: async () => {
      setOpen(null)
      setForm({ name: '', code: '', reason: '' })
      await queryClient.invalidateQueries()
    },
  })
  const failed = nodes.error ?? run.error

  return (
    <>
      <h1>{t('nodeadmin:title')}</h1>
      <p>{t('nodeadmin:explain')}</p>
      {failed != null && (
        <p role="alert" className="error">
          {errorMessage(t, failed)}
        </p>
      )}
      <table className="table">
        <tbody>
          {nodes.data?.map((n) => (
            <tr key={n.id}>
              <td>
                <strong>{n.name}</strong> <small>({n.type})</small>
              </td>
              <td>{n.owner}</td>
              <td>
                <span className={`badge ${n.state === 'active' ? 'ok' : 'off'}`}>{t(`nodeadmin:state.${n.state}`)}</span>
                {n.suspended_reason && <small> — {n.suspended_reason}</small>}
                {n.restorable_until && <small> — {t('nodeadmin:until', { date: new Date(n.restorable_until).toLocaleDateString() })}</small>}
              </td>
              <td>
                {n.state !== 'deleted' && (
                  <button type="button" onClick={() => setOpen({ id: n.id, action: 'emergency' })}>
                    {t('nodeadmin:emergency')}
                  </button>
                )}
                {n.state === 'active' && !n.local && (
                  <button type="button" onClick={() => setOpen({ id: n.id, action: 'suspend' })}>
                    {t('nodeadmin:suspend')}
                  </button>
                )}
                {n.state === 'suspended' && (
                  <button type="button" onClick={() => setOpen({ id: n.id, action: 'resume' })}>
                    {t('nodeadmin:resume')}
                  </button>
                )}
                {n.state === 'deleted' && (
                  <button type="button" onClick={() => setOpen({ id: n.id, action: 'restore' })}>
                    {t('nodeadmin:restore')}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {open && (
        <form
          aria-label={t(`nodeadmin:${open.action}`)}
          onSubmit={(e) => {
            e.preventDefault()
            run.mutate()
          }}
        >
          <h2>{t(`nodeadmin:${open.action}`)}</h2>
          {open.action === 'suspend' && impact.data && (
            <p className="notice">
              {t('nodeadmin:impact', { members: impact.data.members })}{' '}
              {impact.data.worker_connected ? t('nodeadmin:workerOn') : t('nodeadmin:workerOff')}
            </p>
          )}
          {open.action === 'emergency' && <p className="notice">{t('nodeadmin:emergencyWarning')}</p>}
          {(open.action === 'suspend' || open.action === 'emergency') && (
            <label>
              {t('nodeadmin:reason')}
              <small> {t('nodeadmin:reasonShared')}</small>
              <input value={form.reason} required minLength={10} maxLength={300} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
            </label>
          )}
          {open.action !== 'emergency' && (
          <label>
            {t('nodeadmin:typeName')}
            <input value={form.name} required onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          )}
          <label>
            {t('nodeadmin:code')}
            <input value={form.code} required autoComplete="one-time-code" onChange={(e) => setForm({ ...form, code: e.target.value })} />
          </label>
          <div className="actions">
            <button type="submit" className="primary" disabled={run.isPending}>
              {t('nodeadmin:confirm')}
            </button>
            <button type="button" onClick={() => setOpen(null)}>
              {t('nodeadmin:cancel')}
            </button>
          </div>
        </form>
      )}
    </>
  )
}

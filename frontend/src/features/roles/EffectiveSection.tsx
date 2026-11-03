import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../core/api'
import { errorMessage } from '../../core/errors'

interface Line {
  right: string
  scope: string
  expires_at: string | null
  role?: string
  group?: string | null
}

// Everything one account can do, and where each right comes from (own grant, role, group).
export function EffectiveSection({ userId }: { userId: string }) {
  const { t } = useTranslation(['roles', 'errors'])
  const got = useQuery({
    queryKey: ['effective', userId],
    queryFn: () => api.get<{ direct: Line[]; from_roles: Line[] }>(`/users/${userId}/effective`),
  })
  if (got.isPending) return null
  if (got.isError)
    return (
      <p role="alert" className="error">
        {errorMessage(t, got.error)}
      </p>
    )
  const rows = [
    ...got.data.direct.map((l) => ({ ...l, source: t('roles:effective.direct') })),
    ...got.data.from_roles.map((l) => ({
      ...l,
      source: l.group ? t('roles:effective.viaGroup', { role: l.role, group: l.group }) : t('roles:effective.viaRole', { role: l.role }),
    })),
  ]
  return (
    <section>
      <h2>{t('roles:effective.title')}</h2>
      <table className="table">
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.right}-${r.scope}-${i}`}>
              <td>
                <code>{r.right}</code>
              </td>
              <td>{r.scope === '*' ? t('roles:effective.everywhere') : r.scope}</td>
              <td>{r.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

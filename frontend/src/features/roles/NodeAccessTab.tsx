import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../core/api'
import { Problem } from '../../components/Problem'

// "Who can what" on one node: the rights that actually work there for each account.
export function NodeAccessTab({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation(['roles', 'errors'])
  const access = useQuery({
    queryKey: ['node-access', nodeId],
    queryFn: () => api.get<{ user_id: string; username: string; rights: string[] }[]>(`/nodes/${nodeId}/access`),
  })
  if (access.isPending) return <p className="loading">{t('loading')}</p>
  if (access.isError)
    return (
      <Problem error={access.error} />
    )
  return (
    <>
      <p>{t('roles:access.explain')}</p>
      <ul className="links">
        {access.data.map((a) => (
          <li key={a.user_id}>
            <Link to={`/users/${a.user_id}`}>{a.username}</Link>
            <details>
              <summary>{t('roles:includes', { count: a.rights.length })}</summary>
              <p>
                {a.rights.map((r) => (
                  <code key={r}>{r} </code>
                ))}
              </p>
            </details>
          </li>
        ))}
      </ul>
    </>
  )
}

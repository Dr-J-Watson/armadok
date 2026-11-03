import { useTranslation } from 'react-i18next'
import { useSearchParams } from 'react-router-dom'
import { AssignmentsTab } from './AssignmentsTab'
import { GroupsTab } from './GroupsTab'
import { RolesTab } from './RolesTab'

const TABS = ['roles', 'groups', 'assignments'] as const

// Who may do what: roles, groups and the roles given to people and groups.
export function AccessPage() {
  const { t } = useTranslation(['roles'])
  const [params, setParams] = useSearchParams()
  const asked = params.get('tab')
  const tab = TABS.find((x) => x === asked) ?? 'roles'
  return (
    <>
      <h1>{t('roles:title')}</h1>
      <div className="actions">
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            aria-pressed={tab === name}
            className={tab === name ? 'primary' : undefined}
            onClick={() => setParams({ tab: name }, { replace: true })}
          >
            {t(`roles:tabs.${name}`)}
          </button>
        ))}
      </div>
      {tab === 'roles' ? <RolesTab /> : tab === 'groups' ? <GroupsTab /> : <AssignmentsTab />}
    </>
  )
}

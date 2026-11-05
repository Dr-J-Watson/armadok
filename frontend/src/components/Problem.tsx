import { Settings2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '../core/api'
import { codeMessage } from '../core/errors'

// An error shown to a person. Most are real problems (red). A few only mean "this machine is not
// set up for that yet" (a folder was not given to the worker): they are shown as a calm hint with
// what to do, not as an alarm.
export function Problem({ error }: { error: unknown }) {
  const { t } = useTranslation(['errors'])
  const code = error instanceof ApiError ? error.code : 'common.unknown'
  const message = codeMessage(t, code)
  if (/_not_configured$/.test(code)) {
    return (
      <div className="setup-hint" role="status">
        <Settings2 size={18} aria-hidden="true" />
        <span>{message}</span>
      </div>
    )
  }
  return (
    <p role="alert" className="error">
      {message}
    </p>
  )
}

// The server sends stable error codes, never sentences; the interface translates them.

import type { TFunction } from 'i18next'
import { ApiError } from './api'

export function errorMessage(t: TFunction, error: unknown): string {
  const code = error instanceof ApiError ? error.code : 'common.unknown'
  return t(`errors:${code}`, { defaultValue: t('errors:common.unknown') })
}

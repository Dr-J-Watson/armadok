// The server sends stable error codes, never sentences; the interface translates them.

import type { TFunction } from 'i18next'
import { ApiError } from './api'

// For a bare code, e.g. one reported inside an otherwise successful answer.
export function codeMessage(t: TFunction, code: string): string {
  return t(`errors:${code}`, { defaultValue: t('errors:common.unknown') })
}

export function errorMessage(t: TFunction, error: unknown): string {
  return codeMessage(t, error instanceof ApiError ? error.code : 'common.unknown')
}

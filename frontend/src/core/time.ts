// Time checks live here, outside components, so rendering itself stays free of "now".

// A right (or key) with no expiry never expires; otherwise it must still be in the future.
export function stillValid(expiresAt: string | null): boolean {
  return expiresAt === null || new Date(expiresAt).getTime() > Date.now()
}

import { expect, test } from '@playwright/test'

// The Sites and Middlewares pages, with made-up Traefik data (no Traefik needed), and a picture of
// each in test-results for a human look.
const host = (n) => `app${n}.example.com`
const routers = [], services = [], managed = []
for (let i = 1; i <= 9; i += 1) {
  const own = i <= 6
  routers.push({
    id: `app${i}@file`, name: `app${i}`, rule: `Host(\`${host(i)}\`)${i === 3 ? ' && PathPrefix(`/api`)' : ''}`, service: `app${i}@file`,
    entrypoints: ['websecure'], middlewares: i % 3 === 0 ? [] : [i % 2 ? 'chain-private@file' : 'chain-public@file'], tls: true,
    path: own ? `sites/app${i}.yml` : `apps/app${i}.yml`,
  })
  services.push({ id: `app${i}@file`, urls: [i % 2 ? `http://app${i}:8000` : `http://192.0.2.${i}:3000`] })
  if (own) managed.push({
    name: `app${i}`, paused: false, sha256: 'a'.repeat(64), modified_by_hand: false,
    spec: { name: `app${i}`, hosts: [host(i)], path_prefix: null, container: `app${i}`, url: null, port: 8000, scheme: 'http', middlewares: [], https: true, cert_resolver: null, entrypoints: null },
  })
}
managed.push({ name: 'old', paused: true, sha256: 'b'.repeat(64), modified_by_hand: false,
  spec: { name: 'old', hosts: ['old.example.com'], path_prefix: null, container: 'old', url: null, port: 80, scheme: 'http', middlewares: ['lan@file'], https: true, cert_resolver: null, entrypoints: null } })
const mws = [
  { name: 'auth-app', id: 'auth-app@file', type: 'basicAuth', path: 'middlewares/auth/auth-app.yml', refs: [] },
  { name: 'ip-allow-lan', id: 'ip-allow-lan@file', type: 'ipAllowList', path: 'middlewares/ip/ip-allow-lan.yml', refs: [] },
  { name: 'headers-security', id: 'headers-security@file', type: 'headers', path: 'middlewares/headers/headers-security.yml', refs: [] },
  { name: 'errors-custom-pages', id: 'errors-custom-pages@file', type: 'errors', path: 'middlewares/errors-custom-pages.yml', refs: [] },
  { name: 'chain-private', id: 'chain-private@file', type: 'chain', path: 'middlewares/chains/chain-private.yml', refs: ['ip-allow-lan@file', 'headers-security@file'] },
  { name: 'chain-public', id: 'chain-public@file', type: 'chain', path: 'middlewares/chains/chain-public.yml', refs: ['headers-security@file', 'errors-custom-pages@file'] },
]

test('the Sites and Middlewares pages', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Username').fill('admin')
  await page.getByLabel('Password').fill('correct horse battery')
  await page.getByRole('button', { name: 'Log in' }).click()
  await page.route('**/api/v1/nodes/*/traefik/items/overview', (r) => r.fulfill({ json: { routers, services, middlewares: mws, errors: [], truncated: false } }))
  await page.route('**/api/v1/nodes/*/traefik/managed', (r) => r.fulfill({ json: { sites: managed } }))
  await page.route('**/api/v1/nodes/*/traefik/middlewares', (r) => r.fulfill({ json: { middlewares: [] } }))
  await page.route('**/api/v1/nodes/*/dns/report', (r) => r.fulfill({ json: { hosts: routers.map((x, i) => ({ host: host(i + 1), status: i === 2 ? 'missing' : 'present', resolves: [] })) } }))
  await page.route('**/api/v1/nodes/*/certs', (r) => r.fulfill({ json: { certificates: [{ resolver: 'le', names: ['*.example.com'], issuer: 'R11', not_before: '2026-01-01T00:00:00Z', not_after: '2026-12-01T00:00:00Z', serial: '1', days_left: 56, state: 'ok' }], hosts: [], truncated: false } }))
  await page.getByRole('link', { name: 'Nodes', exact: true }).first().click()
  await page.locator('.node-card').first().click()
  await page.waitForURL(/\/nodes\/[^/?]+$/)
  const url = page.url()
  await page.goto(`${url}?tab=traefik`)
  await expect(page.locator('.site-card').first()).toBeVisible()
  await page.setViewportSize({ width: 1400, height: 900 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: 'test-results/sites.png' })
  await page.goto(`${url}?tab=middlewares`)
  await expect(page.locator('.mw-card').first()).toBeVisible()
  await page.screenshot({ path: 'test-results/middlewares.png', fullPage: true })
  await page.route('**/api/v1/nodes/*/traefik/items?*', (r) => r.fulfill({ json: { yaml: 'headers:\n  stsSeconds: 63072000\n  frameDeny: true\n  customResponseHeaders:\n    X-Robots-Tag: noindex\n', sha256: 'c'.repeat(64), disabled: false } }))
  await page.route('**/api/v1/nodes/*/traefik/items/parse', (r) => r.fulfill({ json: { value: { headers: { stsSeconds: 63072000, frameDeny: true, customResponseHeaders: { 'X-Robots-Tag': 'noindex' } } } } }))
  await page.getByRole('button', { name: 'Edit headers-security' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.waitForTimeout(500)
  await page.screenshot({ path: 'test-results/middleware-drawer.png' })
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'New middleware' }).click()
  await page.screenshot({ path: 'test-results/middleware-new.png' })
})

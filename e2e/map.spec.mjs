import { expect, test } from '@playwright/test'

// The map is drawn on a canvas: this checks it shows up with its tools, with made-up relations
// (no Docker needed), and leaves a picture in test-results for a human look.
const objects = [
  ['domain', 'app.example.com'], ['domain', 'blog.example.com'], ['router', 'app'], ['router', 'blog'],
  ['middleware', 'local-only'], ['container', 'app-web-1'], ['container', 'app-db-1'], ['container', 'blog-1'],
  ['project', 'app'], ['project', 'blog'], ['image', 'nginx:1.27'], ['image', 'postgres:16'], ['network', 'app_default'],
  ['volume', 'app_data'], ['volume', 'blog_data'],
].map(([type, id]) => ({ type, id, label: id }))
const link = (a, b, kind) => ({ source: { type: a[0], id: a[1] }, target: { type: b[0], id: b[1] }, kind })
const links = [
  link(['domain', 'app.example.com'], ['router', 'app'], 'serves'), link(['domain', 'blog.example.com'], ['router', 'blog'], 'serves'),
  link(['router', 'app'], ['container', 'app-web-1'], 'routes_to'), link(['router', 'blog'], ['container', 'blog-1'], 'routes_to'),
  link(['router', 'app'], ['middleware', 'local-only'], 'uses'),
  link(['container', 'app-web-1'], ['project', 'app'], 'part_of'), link(['container', 'app-db-1'], ['project', 'app'], 'part_of'),
  link(['container', 'blog-1'], ['project', 'blog'], 'part_of'),
  link(['container', 'app-web-1'], ['image', 'nginx:1.27'], 'runs'), link(['container', 'app-db-1'], ['image', 'postgres:16'], 'runs'),
  link(['container', 'app-db-1'], ['volume', 'app_data'], 'mounts'), link(['container', 'blog-1'], ['volume', 'blog_data'], 'mounts'),
  link(['container', 'app-web-1'], ['network', 'app_default'], 'attached_to'),
]

test('the map shows its columns and tools', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Username').fill('admin')
  await page.getByLabel('Password').fill('correct horse battery')
  await page.getByRole('button', { name: 'Log in' }).click()
  await page.route('**/api/v1/nodes/*/relations', (route) => route.fulfill({ json: { objects, links, unavailable: {} } }))
  await page.getByRole('link', { name: 'Nodes', exact: true }).first().click()
  await page.locator('.node-card').first().click()
  await page.waitForURL(/\/nodes\/[^/?]+$/)
  await page.goto(`${page.url()}?tab=map`)
  await expect(page.getByRole('toolbar')).toBeVisible()
  await page.waitForTimeout(800)
  await page.screenshot({ path: 'test-results/map.png', fullPage: false })
  await page.getByRole('button', { name: 'Full screen' }).click()
  await page.waitForTimeout(400)
  await page.screenshot({ path: 'test-results/map-big.png' })
})

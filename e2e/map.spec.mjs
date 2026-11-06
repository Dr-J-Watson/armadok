import { expect, test } from '@playwright/test'

// The map is drawn on a canvas: this checks it shows up with its tools, with made-up relations
// (no Docker needed), and leaves a picture in test-results for a human look.
const objects = [['network', 'proxy']]
const links = []
const link = (a, b, kind) => links.push({ source: { type: a[0], id: a[1] }, target: { type: b[0], id: b[1] }, kind })
for (let i = 1; i <= 45; i += 1) {
  const p = ['project', `project-${i}`], c = ['container', `project-${i}-web`], img = ['image', `image-${i}:latest`]
  objects.push(p, c, img)
  link(c, p, 'part_of'); link(c, img, 'runs'); link(c, ['network', 'proxy'], 'attached_to')
  if (i % 2) {
    const r = ['router', `site-${i}`], d = ['domain', `site-${i}.example.com`]
    objects.push(r, d); link(d, r, 'serves'); link(r, c, 'routes_to')
    if (i % 3 === 0) { const m = ['middleware', 'local-only']; if (i === 3) objects.push(m); link(r, m, 'uses') }
  }
  if (i % 4 === 0) { const v = ['volume', `data-${i}`]; objects.push(v); link(c, v, 'mounts') }
}
const mapped = objects.map(([type, id]) => ({ type, id, label: id }))

test('the map shows its columns and tools', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Username').fill('admin')
  await page.getByLabel('Password').fill('correct horse battery')
  await page.getByRole('button', { name: 'Log in' }).click()
  await page.route('**/api/v1/nodes/*/relations', (route) => route.fulfill({ json: { objects: mapped, links, unavailable: {} } }))
  await page.getByRole('link', { name: 'Nodes', exact: true }).first().click()
  await page.locator('.node-card').first().click()
  await page.waitForURL(/\/nodes\/[^/?]+$/)
  await page.goto(`${page.url()}?tab=map`)
  await expect(page.locator('.map-canvas')).toBeVisible()
  await page.waitForTimeout(800)
  await page.screenshot({ path: 'test-results/map.png', fullPage: false })
  await page.getByRole('searchbox').fill('project-3')
  await page.screenshot({ path: 'test-results/map-search.png' })
  await page.getByRole('list', { name: 'Search results' }).getByRole('button').first().click()
  await page.waitForTimeout(800)
  await page.screenshot({ path: 'test-results/map-focus.png' })
  await page.getByRole('button', { name: 'Only its surroundings' }).click()
  await page.waitForTimeout(800)
  await page.screenshot({ path: 'test-results/map-only.png' })
})

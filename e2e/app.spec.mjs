import { expect, test } from '@playwright/test'

const PASSWORD = 'correct horse battery'
const USER = 'admin'

async function signIn(page) {
  await page.goto('/')
  await page.getByLabel('Username').fill(USER)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Log in' }).click()
  // Signing in lands on the overview.
  await expect(page.getByRole('heading', { name: `Hello, ${USER}` })).toBeVisible()
}

test.describe.configure({ mode: 'serial' })

test('the first visit asks for the install code and creates the administrator', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel(/install code/i).fill(process.env.INSTALL_CODE)
  await page.getByLabel('Username').fill(USER)
  await page.getByLabel('Password', { exact: true }).fill(PASSWORD)
  await page.getByLabel('Confirm the password').fill(PASSWORD)
  await page.locator('form button[type="submit"]').click()
  // Set-up signs the new administrator in and lands them on the overview.
  await expect(page.getByRole('heading', { name: `Hello, ${USER}` })).toBeVisible()
})

test('a wrong password is refused with a clear message, the right one opens the overview', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Username').fill(USER)
  await page.getByLabel('Password').fill('not the password')
  await page.getByRole('button', { name: 'Log in' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Log in' }).click()
  await expect(page.getByRole('heading', { name: `Hello, ${USER}` })).toBeVisible()
  await expect(page).toHaveTitle(/Overview · Armadok/)
})

test('a node can be added, appears as a card and opens on its own page', async ({ page }) => {
  await signIn(page)
  await page.getByRole('link', { name: 'Nodes', exact: true }).first().click()
  await page.getByRole('button', { name: 'Add a node' }).click()
  await page.getByLabel('Name').fill('Garage server')
  await page.getByRole('button', { name: 'Add the node' }).click()
  const card = page.getByRole('link', { name: /Garage server/ })
  await expect(card).toBeVisible()
  await card.click()
  await expect(page.getByRole('heading', { name: /Garage server/ })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true')
  // The dangerous part is folded away until asked for.
  await expect(page.getByRole('button', { name: 'Delete the node' })).toBeHidden()
  await page.getByText('Danger zone').click()
  await expect(page.getByRole('button', { name: 'Delete the node' })).toBeVisible()
})

test('the look can be changed from the account menu and is remembered', async ({ page }) => {
  await signIn(page)
  await page.locator('.account-button').click()
  await page.getByRole('button', { name: 'Dark' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.locator('.account-button').click()
  await page.getByRole('button', { name: '#7b4bd6' }).click()
  const accent = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim())
  expect(accent).toBe('#7b4bd6')
  await page.getByRole('button', { name: 'System' }).click()
})

test('the search opens with Ctrl+K, finds a node and goes to it', async ({ page }) => {
  await signIn(page)
  await page.keyboard.press('Control+k')
  const box = page.getByRole('combobox', { name: /A page, a node/ })
  await expect(box).toBeFocused()
  await box.fill('Garage')
  await page.getByRole('listbox').getByRole('option', { name: /Garage server/ }).click()
  await expect(page.getByRole('heading', { name: /Garage server/ })).toBeVisible()
})

test('the live connection is opened once and stays open while moving around', async ({ page }) => {
  // A connection that restarted at every redraw made the status light blink and every screen
  // reload: count the sockets the browser opens.
  let sockets = 0
  page.on('websocket', () => sockets++)
  await signIn(page)
  await page.getByRole('link', { name: 'Nodes', exact: true }).first().click()
  await page.getByRole('link', { name: 'DNS', exact: true }).first().click()
  await page.waitForTimeout(3000)
  expect(sockets).toBe(1)
})

test('an unknown address says so kindly and links back', async ({ page }) => {
  await signIn(page)
  await page.goto('/nothing-here')
  await expect(page.getByRole('heading', { name: 'This page does not exist' })).toBeVisible()
  await page.getByRole('link', { name: 'Overview' }).last().click()
  await expect(page.getByRole('heading', { name: `Hello, ${USER}` })).toBeVisible()
})

test('on a phone the menu is a drawer that opens, navigates and closes', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'en-GB' })
  const page = await context.newPage()
  await signIn(page)
  const menu = page.getByRole('button', { name: 'Menu' })
  await expect(page.locator('.sidebar')).not.toBeInViewport()
  await menu.click()
  await expect(page.locator('.sidebar')).toBeInViewport()
  await page.getByRole('link', { name: 'Nodes', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Nodes', exact: true })).toBeVisible()
  await expect(page.locator('.sidebar')).not.toBeInViewport()
  // No horizontal scrolling on a phone.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
  expect(overflow).toBe(false)
  await context.close()
})

test('signing out returns to the sign-in page', async ({ page }) => {
  await signIn(page)
  await page.locator('.account-button').click()
  await page.getByRole('menuitem', { name: 'Log out' }).click()
  await expect(page.getByRole('button', { name: 'Log in' })).toBeVisible()
})

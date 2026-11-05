import { defineConfig } from '@playwright/test'

// One browser, one worker: the tests share one throwaway instance and run in order (the first
// one sets it up).
export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.mjs',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 30_000,
  reporter: [['list']],
  use: { baseURL: process.env.BASE_URL, locale: 'en-GB', screenshot: 'only-on-failure', trace: 'off' },
})

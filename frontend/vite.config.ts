import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// In development the interface runs on its own port and forwards API calls (including the
// worker WebSocket) to the hub; in production the hub serves the built files itself.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8000', ws: true } } },
  test: { environment: 'jsdom', globals: true, setupFiles: ['./src/test/setup.ts'] },
})

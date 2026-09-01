/// <reference types="vitest" />
// frontend/vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Without this, vitest globs the whole project and collects the Playwright
  // specs under e2e/ — which use Playwright's `test.describe`, not vitest's —
  // and fails to parse them. Unit tests are vitest and live beside the source;
  // e2e is Playwright and runs separately via `npm run test:e2e`.
  // Environment stays at the default ('node') because there are no tests yet and
  // jsdom is not a dependency. The first React component test needs
  // `npm i -D jsdom` and `environment: 'jsdom'` here.
  test: {
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    exclude: ['node_modules/**', 'dist/**', 'e2e/**'],
  },
  server: {
    port: 5173,
    host: true,
    allowedHosts: process.env.VITE_ALLOWED_HOSTS?.split(',') || true,
    proxy: {
      '/api': {
        target: 'http://api:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})

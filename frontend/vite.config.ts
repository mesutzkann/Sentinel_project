import react from '@vitejs/plugin-react'
// From vitest/config, not vite: the `test` block below is not part of Vite's own
// config type, and importing defineConfig from vite makes `tsc -b` reject it.
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],

  test: {
    // jsdom rather than a real browser: these tests are about what the page renders from a
    // given API response, which is the part that breaks when a contract shifts. Nothing here
    // depends on layout or on a real network.
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})

import { defineConfig, devices } from '@playwright/test';

/**
 * The same hermetic harness as `playwright.workbench-general.config.ts`, against
 * the built app instead of the dev server.
 *
 * `mobile-continuation.spec.ts` drives the workspace chip's manager branch, and
 * the directory browser inside it cannot be driven on the dev server: React's
 * StrictMode mounts, cleans up and re-mounts every component in development, and
 * `DirectoryBrowser`'s `mountedRef` is only ever set to `false` by that cleanup —
 * never back to `true` — so the browse response is discarded and the picker sits
 * in a permanent loading state. That is a development-only trap in a shared
 * primitive this PR does not touch (StrictMode is inert in a production build),
 * so the spec runs against the bundle users actually get rather than working
 * around it here or changing that component.
 *
 * The build runs as part of the server command so the spec can never grade a
 * stale `dist/`. Port and origin match the dev config because the spec's request
 * guard is written against that one origin.
 */
export default defineConfig({
  testDir: './e2e/workbench-general',
  testMatch: ['**/mobile-continuation.spec.ts'],
  outputDir: './e2e/.artifacts/workbench-general/run',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5213', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run build && npx vite preview --host 127.0.0.1 --port 5213 --strictPort',
    url: 'http://127.0.0.1:5213/',
    timeout: 180_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

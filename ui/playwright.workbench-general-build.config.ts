import { defineConfig, devices } from '@playwright/test';

/**
 * The same hermetic harness as `playwright.workbench-general.config.ts`, against
 * the built app instead of the dev server.
 *
 * `composer-mention-picker.spec.ts` is here because of React's StrictMode: on the
 * dev server the `#`/`@` typeahead's first open in a freshly mounted editor
 * often never renders its list, so a spec about where that list lands would
 * grade the development double-mount rather than the picker. The built app
 * opens it every time.
 *
 * The build runs as part of the server command so the spec can never grade a
 * stale `dist/`. Port and origin match the dev config because the spec's request
 * guard is written against that one origin.
 */
export default defineConfig({
  testDir: './e2e/workbench-general',
  testMatch: ['**/composer-mention-picker.spec.ts'],
  outputDir: './e2e/.artifacts/workbench-general/run',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5213', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run build && npx vite preview --host 127.0.0.1 --port 5213 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5213/',
    timeout: 180_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

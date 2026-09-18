import { defineConfig, devices } from '@playwright/test';

// Hermetic: the dev server is bound to loopback and points at a dead backend port,
// and every request the page makes is answered or refused by the spec's own routes,
// so nothing here reaches a running Avibe service or writes to user state.
export default defineConfig({
  testDir: './e2e/workbench-general',
  // Run against the built app instead — see playwright.workbench-general-build.config.ts.
  testIgnore: ['**/mobile-continuation.spec.ts', '**/setup-handoff.spec.ts'],
  // Playwright empties `outputDir` at the start of every run, including a filtered
  // one, so the captures must NOT live inside it: a `-g` re-run of two shots would
  // otherwise delete the other sixteen. Traces get their own subdirectory and the
  // shots keep a stable path of their own.
  outputDir: './e2e/.artifacts/workbench-general/run',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5213', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5213 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5213/',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

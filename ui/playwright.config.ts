// Playwright configuration for the Model Hub web-interaction suite.
//
// The suite drives a LIVE Avibe instance over HTTP — there is no `webServer`
// block on purpose. Starting the instance is the operator's step (see
// `e2e/README.md`), because the thing under test is a stateful local runtime
// with an installed gateway engine, not a process a test run may create and
// discard. `VIBE_E2E_BASE_URL` names that instance.
//
// `VIBE_E2E_BASE_URL` has no default, and reading it here is what enforces
// that: the whole run refuses to load without it. See `e2e/support/env.ts` for
// why — the suite mutates the instance it is pointed at, and the port it would
// otherwise default to is a developer's real `vibe`.
import { defineConfig, devices } from '@playwright/test';

import { BASE_URL } from './e2e/support/env';

export default defineConfig({
  testDir: './e2e',
  outputDir: './e2e/.artifacts/test-results',
  // Every spec mutates shared instance state (sources, agent modes, the
  // runtime switch). Parallel workers would race on it, so the suite is serial
  // by construction rather than by luck.
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [
    ['list'],
    ['html', { outputFolder: './e2e/.artifacts/report', open: 'never' }],
  ],
  use: {
    baseURL: BASE_URL,
    // Pins the language the copy assertions read from `src/i18n/en.json`.
    // i18next detects `localStorage` then `navigator`; a fresh Playwright
    // context has no stored preference, so the navigator locale decides.
    locale: 'en-US',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

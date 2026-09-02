// Playwright configuration for the Model Hub web-interaction suite.
//
// The suite drives a LIVE Avibe instance over HTTP — there is no `webServer`
// block on purpose. Starting the instance is the operator's step (see
// `e2e/README.md`), because the thing under test is a stateful local runtime
// with an installed gateway engine, not a process a test run may create and
// discard. `VIBE_E2E_BASE_URL` names that instance.
//
// The default matches the frozen cross-lane vocabulary (test plan §5a). It is
// also the port a developer's own `vibe` service listens on, which is exactly
// why the README tells you to point the variable at a hermetic instance instead
// of relying on the default.
import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.VIBE_E2E_BASE_URL ?? 'http://127.0.0.1:5123';

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
    baseURL,
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

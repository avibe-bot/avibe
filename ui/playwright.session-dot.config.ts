import { defineConfig, devices } from '@playwright/test';

// Hermetic: the dev server is bound to loopback and points at a dead backend
// port, and the fixture answers every request in-page, so nothing here reaches
// a running Avibe service or writes to user state.
export default defineConfig({
  testDir: './e2e/session-dot',
  outputDir: './e2e/.artifacts/session-dot',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5214', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5214 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5214/e2e/session-dot/fixture.html',
    reuseExistingServer: true,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});

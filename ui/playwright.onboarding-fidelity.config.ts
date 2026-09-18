import { defineConfig, devices } from '@playwright/test';

// Hermetic: the dev server is bound to loopback and points at a dead backend port,
// and every request the page makes is answered or refused by the spec's own routes,
// so nothing here reaches a running Avibe service or writes to user state.
export default defineConfig({
  testDir: './e2e/onboarding-fidelity',
  outputDir: './e2e/.artifacts/onboarding-fidelity',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5212', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5212 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5212/e2e/onboarding-fidelity/fixture.html',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    {
      name: 'webkit-narrow',
      grep: /narrow identity alignment/,
      use: { ...devices['Desktop Safari'] },
    },
  ],
});

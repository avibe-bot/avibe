import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/inbox-return',
  outputDir: './e2e/.artifacts/inbox-return',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5199', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5199 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5199/e2e/inbox-return/fixture.html',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});

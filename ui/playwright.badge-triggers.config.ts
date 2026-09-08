import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/badge-triggers',
  outputDir: './e2e/.artifacts/badge-triggers',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5211', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5211 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5211/e2e/badge-triggers/fixture.html',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
    { name: 'mobile-webkit', use: { ...devices['iPhone 13'] } },
  ],
});

import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/citations',
  outputDir: './e2e/.artifacts/citations',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5218', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5218 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5218/e2e/citations/fixture.html',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
    { name: 'mobile-webkit', use: { ...devices['iPhone 13'] } },
  ],
});

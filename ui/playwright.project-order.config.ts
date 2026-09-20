import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/project-order',
  outputDir: './e2e/.artifacts/project-order',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5208', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5208 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5208/e2e/project-order/fixture.html',
    reuseExistingServer: true,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});

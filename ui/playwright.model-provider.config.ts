import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/model-provider',
  outputDir: './e2e/.artifacts/model-provider',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5207', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5207 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5207/e2e/model-provider/fixture.html',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});

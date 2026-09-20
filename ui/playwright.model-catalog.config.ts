import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/model-catalog',
  outputDir: './e2e/.artifacts/model-catalog',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5201', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5201 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5201/e2e/model-catalog/fixture.html?backend=opencode',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});

import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/chat-selection',
  outputDir: './e2e/.artifacts/chat-selection',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5219', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5219 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5219/e2e/chat-selection/fixture.html',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'desktop-webkit', use: { ...devices['Desktop Safari'] } },
  ],
});

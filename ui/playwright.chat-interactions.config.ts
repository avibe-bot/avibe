import { defineConfig, devices } from '@playwright/test';

// Real chat components and CSS, with all backend reads answered by the suite.
export default defineConfig({
  testDir: './e2e/chat-interactions',
  outputDir: './e2e/.artifacts/chat-interactions',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5227', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5227 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5227/e2e/chat-interactions/fixture.html',
  },
  projects: [
    { name: 'chromium-touch', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
    { name: 'webkit-touch', use: { ...devices['iPhone 13'], browserName: 'webkit' } },
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
  ],
});

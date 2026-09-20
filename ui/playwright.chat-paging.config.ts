import { defineConfig, devices } from '@playwright/test';

// This suite renders the real Transcript with an in-memory pagination owner.
// It has no Avibe backend, credentials, or persistent application state.
export default defineConfig({
  testDir: './e2e/chat-paging',
  outputDir: './e2e/.artifacts/chat-paging',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5198', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5198 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5198/e2e/chat-paging/fixture.html',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});

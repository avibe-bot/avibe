import { defineConfig, devices } from '@playwright/test';

// The shipped ChatPage in the shipped phone shell, its reads answered in memory.
// No Avibe backend, credentials, or persistent application state.
export default defineConfig({
  testDir: './e2e/chat-scroll',
  outputDir: './e2e/.artifacts/chat-scroll',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5241', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5241 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5241/e2e/chat-scroll/fixture.html',
  },
  projects: [
    { name: 'chromium-touch', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
    // Playwright's mobile WebKit takes no wheel or touch scrolling, so WebKit
    // runs at the phone's size with the wheel as its scroll input.
    { name: 'webkit', use: { browserName: 'webkit', viewport: { width: 390, height: 664 }, deviceScaleFactor: 2 } },
  ],
});

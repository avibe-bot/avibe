import { defineConfig, devices } from '@playwright/test';

// Hermetic: the dev server points at a dead backend port and every
// `/api/media/...` request is answered by the spec with a generated PDF.
//
// The page reader is what phones and tablets get, so it runs on an Android
// device in Chromium and on an iPhone in WebKit (the engine whose framed viewer
// showed only page one). The desktop project checks which reader a desktop
// browser is given.
export default defineConfig({
  testDir: './e2e/pdf-preview',
  outputDir: './e2e/.artifacts/pdf-preview',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5231', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5231 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5231/e2e/pdf-preview/fixture.html',
    reuseExistingServer: true,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'android', use: { ...devices['Pixel 7'] } },
    { name: 'iphone-webkit', use: { ...devices['iPhone 13'] } },
  ],
});

import { defineConfig, devices } from '@playwright/test';

// Motion of the shared surfaces (docs/plans/web-ui-motion.md): hermetic, against a fixture page.
export default defineConfig({
  testDir: './e2e/motion',
  outputDir: './e2e/.artifacts/motion',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5263', locale: 'en-US', trace: 'retain-on-failure' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5263 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5263/e2e/motion/fixture.html',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
    { name: 'mobile-webkit', use: { ...devices['iPhone 13'] } },
  ],
});

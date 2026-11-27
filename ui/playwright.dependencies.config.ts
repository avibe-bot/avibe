import { defineConfig, devices } from '@playwright/test';

// Run `npm run build` first. All API traffic is intercepted by the read-only
// fixture; the preview server never connects to an installed Runtime.
export default defineConfig({
  testDir: './e2e/workbench-general',
  testMatch: '**/dependency-recovery.spec.ts',
  outputDir: './e2e/.artifacts/dependencies/run',
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5213', trace: 'retain-on-failure' },
  webServer: {
    command: 'npx vite preview --host 127.0.0.1 --port 5213 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5213/',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'], launchOptions: {
      ...(process.env.PLAYWRIGHT_WEBKIT_EXECUTABLE_PATH
        ? { executablePath: process.env.PLAYWRIGHT_WEBKIT_EXECUTABLE_PATH } : {}),
    } } },
  ],
});

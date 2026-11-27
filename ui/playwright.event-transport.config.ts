import { defineConfig, devices } from '@playwright/test';

// A fresh HTTP/1.1 server and browser context belong to each test. Build the
// fixture first; this suite never starts Avibe or uses a persistent profile.
export default defineConfig({
  testDir: './e2e/event-transport',
  outputDir: './e2e/.artifacts/event-transport',
  workers: 1,
  retries: 0,
  timeout: 30_000,
  use: { trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    {
      name: 'webkit',
      use: {
        ...devices['Desktop Safari'],
        ...(process.env.EVENT_TRANSPORT_WEBKIT
          ? { launchOptions: { executablePath: process.env.EVENT_TRANSPORT_WEBKIT } } : {}),
      },
    },
  ],
});

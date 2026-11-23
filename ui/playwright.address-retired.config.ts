import { defineConfig, devices } from '@playwright/test';

// The spec runs its own local server that replays the instance's fixed answers
// for a retired address; nothing else is started.
export default defineConfig({
  testDir: './e2e/address-retired',
  outputDir: './e2e/.artifacts/address-retired',
  workers: 1,
  retries: 0,
  use: { trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});

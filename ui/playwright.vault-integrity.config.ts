import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/vault-integrity',
  outputDir: process.env.AVIBE_TEST_ARTIFACT_DIR ?? './e2e/.artifacts/vault-integrity',
  timeout: 45_000,
  workers: 1,
  retries: 0,
  use: { baseURL: 'http://127.0.0.1:5238' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5238 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5238/e2e/vault-integrity/fixture.html',
    reuseExistingServer: false,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});

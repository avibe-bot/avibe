import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/pet-mini',
  outputDir: './e2e/.artifacts/pet-mini',
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:5229',
    viewport: { width: 460, height: 600 },
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5229 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5229/e2e/pet-mini/fixture.html',
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'webkit', use: { browserName: 'webkit' } },
  ],
});

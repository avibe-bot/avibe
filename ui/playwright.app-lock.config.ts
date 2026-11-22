import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/desktop-client',
  testMatch: 'app-lock.spec.ts',
  outputDir: './e2e/.artifacts/app-lock',
  workers: 1,
  retries: 0,
  use: { trace: 'retain-on-failure', baseURL: 'http://127.0.0.1:5267' },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5267 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5267',
    reuseExistingServer: false,
  },
});

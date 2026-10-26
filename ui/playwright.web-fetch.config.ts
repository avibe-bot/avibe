import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/web-fetch', outputDir: './e2e/.artifacts/web-fetch', workers: 1, retries: 0,
  use: { baseURL: 'http://127.0.0.1:5218', trace: 'retain-on-failure' },
  webServer: { command: 'npm run dev -- --host 127.0.0.1 --port 5218 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' }, url: 'http://127.0.0.1:5218/e2e/web-fetch/fixture.html' },
  projects: [{ name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 960 } } }],
});

import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/desktop-client',
  outputDir: './e2e/.artifacts/desktop-client',
  workers: 1,
  retries: 0,
  use: { trace: 'retain-on-failure', baseURL: 'http://127.0.0.1:5213' },
  webServer: [
    { command: 'npm run dev -- --host 127.0.0.1 --port 5213 --strictPort', env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' }, url: 'http://127.0.0.1:5213', reuseExistingServer: false },
    { command: 'node ../desktop/node_modules/vite/bin/vite.js preview ../desktop --config ../desktop/vite.config.ts --host 127.0.0.1 --port 5214 --strictPort', url: 'http://127.0.0.1:5214/account/index.html', reuseExistingServer: false },
  ],
});

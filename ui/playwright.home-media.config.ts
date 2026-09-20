import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/home-media',
  outputDir: './e2e/.artifacts/home-media',
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:5217',
    trace: 'retain-on-failure',
    permissions: ['microphone'],
    launchOptions: { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] },
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5217 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5217/e2e/home-media/fixture.html',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

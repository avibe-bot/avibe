import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e/voice-shortcut',
  outputDir: './e2e/.artifacts/voice-shortcut',
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:5218',
    permissions: ['microphone'],
    launchOptions: {
      args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
    },
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5218 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5218/e2e/voice-shortcut/fixture.html',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

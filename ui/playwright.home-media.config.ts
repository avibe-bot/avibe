import { defineConfig, devices } from '@playwright/test';

import type { VoiceWorkletOptions } from './e2e/support/browserTest';

export default defineConfig<VoiceWorkletOptions>({
  testDir: './e2e/home-media',
  outputDir: './e2e/.artifacts/home-media',
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:5217',
    trace: 'retain-on-failure',
    permissions: ['microphone'],
    launchOptions: { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] },
    // The microphone cases capture through the composer's real voice worklet
    // (support/browserTest stubs it by default).
    voiceWorklet: 'real',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5217 --strictPort',
    env: { VIBE_UI_BACKEND: 'http://127.0.0.1:9' },
    url: 'http://127.0.0.1:5217/e2e/home-media/fixture.html',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});

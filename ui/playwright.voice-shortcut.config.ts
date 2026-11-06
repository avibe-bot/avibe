import { existsSync, mkdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';

import type { VoiceWorkletOptions } from './e2e/support/browserTest';

const outputDir = './e2e/.artifacts/voice-shortcut';

/** A continuous tone at about −12 dBFS, so a fake microphone is loud from its first sample (the
 *  default fake device beeps once a second, which would hide how soon a meter shows a level). */
const toneFile = (() => {
  // Beside the run's output, which Playwright may clear, under the same ignored directory.
  const file = resolve(dirname(fileURLToPath(import.meta.url)), 'e2e/.artifacts/voice-shortcut-tone.wav');
  const rate = 48_000;
  const samples = rate * 4;
  if (existsSync(file) && statSync(file).size === 44 + samples * 2) return file;
  const data = Buffer.alloc(44 + samples * 2);
  data.write('RIFF', 0);
  data.writeUInt32LE(36 + samples * 2, 4);
  data.write('WAVEfmt ', 8);
  data.writeUInt32LE(16, 16);
  data.writeUInt16LE(1, 20);
  data.writeUInt16LE(1, 22);
  data.writeUInt32LE(rate, 24);
  data.writeUInt32LE(rate * 2, 28);
  data.writeUInt16LE(2, 32);
  data.writeUInt16LE(16, 34);
  data.write('data', 36);
  data.writeUInt32LE(samples * 2, 40);
  for (let index = 0; index < samples; index += 1) {
    data.writeInt16LE(Math.round(Math.sin((2 * Math.PI * 440 * index) / rate) * 8_000), 44 + index * 2);
  }
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, data);
  return file;
})();

export default defineConfig<VoiceWorkletOptions>({
  testDir: './e2e/voice-shortcut',
  outputDir,
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
  projects: [
    // The microphone projects keep the composer's real voice worklet: they capture through it
    // (support/browserTest stubs it everywhere else).
    { name: 'chromium', testIgnore: /meter\.spec\.ts/, use: { ...devices['Desktop Chrome'], voiceWorklet: 'real' } },
    // The meter, on a microphone that is never quiet.
    {
      name: 'meter',
      testMatch: /meter\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        voiceWorklet: 'real',
        launchOptions: {
          args: [
            '--use-fake-device-for-media-stream',
            '--use-fake-ui-for-media-stream',
            `--use-file-for-fake-audio-capture=${toneFile}`,
          ],
        },
      },
    },
    // A click on the composer is Safari's as much as Chrome's; the microphone suites need
    // Chromium's fake media device.
    {
      name: 'webkit',
      testMatch: /caret\.spec\.ts/,
      use: { ...devices['Desktop Safari'], permissions: [], launchOptions: {} },
    },
  ],
});

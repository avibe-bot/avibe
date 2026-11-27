import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const directory = dirname(fileURLToPath(import.meta.url));
const sdkDist = process.env.SHOW_SDK_DIST;
if (!sdkDist) throw new Error('SHOW_SDK_DIST must name the companion SDK dist directory');
const workbench = resolve(directory, '../../src/lib/workbenchEventSource.ts');
const sha256 = (path: string) => createHash('sha256').update(readFileSync(path)).digest('hex');

export default defineConfig({
  resolve: { alias: { '@event-transport/show-sdk': resolve(sdkDist, 'index.js') } },
  build: {
    outDir: resolve(directory, '.build'),
    emptyOutDir: true,
    minify: true,
    lib: { entry: resolve(directory, 'main.ts'), name: 'EventTransportFixture', formats: ['iife'], fileName: () => 'fixture.js' },
  },
  plugins: [{
    name: 'event-transport-evidence-provenance',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'provenance.json',
        source: JSON.stringify({
          builtAt: new Date().toISOString(),
          avibeHead: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: directory, encoding: 'utf8' }).trim(),
          workbenchSourceSha256: sha256(workbench),
          sdkHead: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: sdkDist, encoding: 'utf8' }).trim(),
          sdkIndexSha256: sha256(resolve(sdkDist, 'index.js')),
          sdkTransportSha256: sha256(resolve(sdkDist, 'event-stream.js')),
        }, null, 2),
      });
    },
  }],
});

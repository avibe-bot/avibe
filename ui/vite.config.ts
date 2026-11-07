import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import { configDefaults, defineConfig } from 'vitest/config'

import { emojiCatalog } from './scripts/emojiCatalog.ts'
import { firstScreenPreload } from './scripts/firstScreenPreload.ts'

// Point the dev proxy at any running Avibe instance — e.g. the Incus regression
// environment — with VIBE_UI_BACKEND=http://127.0.0.1:15130 npm run dev, so UI
// work can be driven against real data without restarting the local service.
const backendTarget = process.env.VIBE_UI_BACKEND ?? 'http://localhost:5100'

const backendProxy = () => ({
  target: backendTarget,
  changeOrigin: true,
  configure(proxy: {
    on: (
      event: 'proxyReq',
      handler: (proxyReq: { setHeader: (name: string, value: string) => void }) => void,
    ) => void
  }) {
    // Codex's in-app browser maps the Vite port to a temporary localhost
    // origin. Rewrite the development proxy headers so Avibe's same-origin
    // CSRF validation sees the isolated backend origin, while production keeps
    // using the normal browser origin unchanged.
    proxy.on('proxyReq', (proxyReq) => {
      proxyReq.setHeader('origin', backendTarget)
      proxyReq.setHeader('referer', `${backendTarget}/`)
    })
  },
})

// https://vite.dev/config/
export default defineConfig({
  // Vitest's default `include` is `**/*.{test,spec}.?(c|m)[jt]s?(x)`, which
  // matches `e2e/*.spec.ts` as readily as `src/*.test.tsx`. Those files are
  // Playwright's, and calling `test.describe()` under Vitest throws
  // ("Playwright Test did not expect test.describe() to be called here"), so
  // without this the unit suite fails on files it was never meant to collect.
  // Excluding the directory — rather than renaming the specs — keeps the trap
  // disarmed for every Playwright file added later.
  test: { exclude: [...configDefaults.exclude, 'e2e/**'] },
  plugins: [react(), emojiCatalog(), firstScreenPreload()],
  build: {
    rolldownOptions: {
      output: {
        // A `lib` chunk is named after the pages that share it (`lib~ChatPage~…`);
        // its file is named by content alone, in the characters the previous-build
        // archive serves (vibe/ui_asset_archive.py).
        chunkFileNames: (chunk) => (chunk.name.startsWith('lib~') ? 'assets/lib-[hash].js' : 'assets/[name]-[hash].js'),
        assetFileNames: (asset) => (asset.names.some((name) => name.startsWith('lib~'))
          ? 'assets/lib-[hash][extname]'
          : 'assets/[name]-[hash][extname]'),
        // docs/plans/web-ui-first-load.md section 3.
        codeSplitting: {
          groups: [
            // What the first screen loads (`$initial`: the entry's static
            // imports) is two files rather than a spray of shared fragments:
            // the libraries, which change only with a dependency update and so
            // stay cached across releases, and the app shell. Vite's preload
            // helper rides with the libraries, so a lazy chunk that loads
            // another does not import the shell for it.
            { name: 'vendor', test: /[\\/]node_modules[\\/]|^\0vite\/preload-helper/, tags: ['$initial'], priority: 2 },
            { name: 'shell', tags: ['$initial'], priority: 1 },
            // Libraries only lazy pages use, grouped by the pages that use them.
            // They import only other libraries, so a release that changes app
            // code leaves them cached.
            { name: 'lib', test: /[\\/]node_modules[\\/]/, entriesAware: true, priority: 0 },
          ],
        },
      },
    },
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // Monaco copies DOMPurify into its package; an npm override does not
      // replace that file. Route its exact ESM import to our patched dependency
      // in both dependency prebundling and production builds.
      './dompurify/dompurify.js': fileURLToPath(new URL('./node_modules/dompurify/dist/purify.es.mjs', import.meta.url)),
    },
  },
  server: {
    fs: {
      // Two shipped catalogs are read by both the Web UI and a Python module, from
      // one tracked file each rather than from a copy per reader:
      // ``src/lib/messageTypes.ts`` imports ``vibe/message_types.json``, and
      // ``src/components/settings/models/apiKeyVendors.ts`` imports
      // ``vibe/data/api_key_vendors.json``, and ``vibe/data/media_types.json``.
      // ``ui/package-lock.json`` makes Vite infer ``ui/`` as the workspace root, which
      // would put both files outside the dev server's default allow list; production
      // builds inline the JSON and are unaffected. Allow the UI root plus those exact
      // files — not their common ancestor, which would serve the rest of the checkout
      // over ``/@fs/`` to anything that can reach the dev server.
      allow: [
        fileURLToPath(new URL('.', import.meta.url)),
        fileURLToPath(new URL('../vibe/message_types.json', import.meta.url)),
        fileURLToPath(new URL('../vibe/data/api_key_vendors.json', import.meta.url)),
        fileURLToPath(new URL('../vibe/data/media_types.json', import.meta.url)),
        fileURLToPath(new URL('../vibe/data/vault_sandbox_release.json', import.meta.url)),
      ],
    },
    proxy: {
      '/config': backendProxy(),
      '/session': backendProxy(),
      '/status': backendProxy(),
      '/settings': backendProxy(),
      '/logs': backendProxy(),
      '/doctor': backendProxy(),
      '/remote-access': backendProxy(),
      '/control': backendProxy(),
      '/upgrade': backendProxy(),
      '/api': backendProxy(),
    },
  },
})

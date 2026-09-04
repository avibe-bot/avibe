import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import { configDefaults, defineConfig } from 'vitest/config'

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
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    fs: {
      // Two shipped catalogs are read by both the Web UI and a Python module, from
      // one tracked file each rather than from a copy per reader:
      // ``src/lib/messageTypes.ts`` imports ``vibe/message_types.json``, and
      // ``src/components/settings/models/apiKeyVendors.ts`` imports
      // ``vibe/data/api_key_vendors.json``.
      // ``ui/package-lock.json`` makes Vite infer ``ui/`` as the workspace root, which
      // would put both files outside the dev server's default allow list; production
      // builds inline the JSON and are unaffected. Allow the UI root plus those exact
      // files — not their common ancestor, which would serve the rest of the checkout
      // over ``/@fs/`` to anything that can reach the dev server.
      allow: [
        fileURLToPath(new URL('.', import.meta.url)),
        fileURLToPath(new URL('../vibe/message_types.json', import.meta.url)),
        fileURLToPath(new URL('../vibe/data/api_key_vendors.json', import.meta.url)),
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

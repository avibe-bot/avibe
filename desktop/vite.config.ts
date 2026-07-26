import { defineConfig } from 'vite'

import en from '../ui/src/i18n/en.json'
import zh from '../ui/src/i18n/zh.json'

// The dev server port is part of the shell's trust boundary: `is_shell_ui_url`
// in `runtime-host` accepts `http://localhost:1420` as the shell's own page, and
// only in debug builds. Keep this in sync with `DEV_SERVER_PORT` and with
// `build.devUrl` in `src-tauri/tauri.conf.json`.
const DEV_SERVER_PORT = 1420

export default defineConfig({
  // The bootstrap must render while the Runtime is down. Inject only its small
  // section from the central catalogs instead of bundling the whole Workbench
  // translation payload or fetching it from loopback.
  define: {
    __DESKTOP_BOOTSTRAP_CATALOGS__: JSON.stringify({
      en: en.desktopBootstrap,
      zh: zh.desktopBootstrap,
    }),
  },
  // Tauri prints its own diagnostics; do not wipe them.
  clearScreen: false,
  server: {
    host: '127.0.0.1',
    port: DEV_SERVER_PORT,
    strictPort: true,
  },
  build: {
    // The bootstrap page only ever runs in the bundled WebView (WKWebView on
    // macOS, WebView2 on Windows), so it does not need legacy output.
    target: ['es2022', 'safari15', 'chrome110'],
    emptyOutDir: true,
  },
})

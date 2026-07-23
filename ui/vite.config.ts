import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backendTarget = 'http://localhost:5100'

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
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
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

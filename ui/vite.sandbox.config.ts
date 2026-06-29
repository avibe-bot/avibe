import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  root: fileURLToPath(new URL('./sandbox', import.meta.url)),
  publicDir: false,
  build: {
    outDir: fileURLToPath(new URL('./dist-sandbox', import.meta.url)),
    emptyOutDir: true,
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
})

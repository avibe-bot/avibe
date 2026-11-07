// LAB (lab/disclosure-variants): builds the disclosure lab (lab/disclosure) into a static bundle that
// any static host serves from any path, such as a Show Page's public/lab.
import { execSync } from 'node:child_process'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

import { emojiCatalog } from './scripts/emojiCatalog.ts'

const git = (args: string) => execSync(`git ${args}`, { encoding: 'utf8' }).trim()

export default defineConfig({
  root: fileURLToPath(new URL('./lab/disclosure', import.meta.url)),
  base: './',
  publicDir: false,
  plugins: [react(), emojiCatalog()],
  define: {
    __LAB_SOURCE__: JSON.stringify({
      branch: git('rev-parse --abbrev-ref HEAD'),
      head: git('rev-parse HEAD'),
      built: new Date().toISOString().slice(0, 16).replace('T', ' '),
    }),
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      './dompurify/dompurify.js': fileURLToPath(new URL('./node_modules/dompurify/dist/purify.es.mjs', import.meta.url)),
    },
  },
  server: {
    fs: {
      allow: [
        fileURLToPath(new URL('.', import.meta.url)),
        fileURLToPath(new URL('../vibe/message_types.json', import.meta.url)),
        fileURLToPath(new URL('../vibe/data/api_key_vendors.json', import.meta.url)),
        fileURLToPath(new URL('../vibe/data/media_types.json', import.meta.url)),
        fileURLToPath(new URL('../vibe/data/vault_sandbox_release.json', import.meta.url)),
      ],
    },
  },
  build: {
    outDir: fileURLToPath(new URL('./dist-lab', import.meta.url)),
    emptyOutDir: true,
    chunkSizeWarningLimit: 8000,
  },
})

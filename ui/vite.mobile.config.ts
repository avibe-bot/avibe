import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/postcss';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

import { bundleNotices } from './scripts/bundleNotices.mjs';

// The third-party notices of exactly what this bundle ships, for Account → Open-source licenses.
const notices = bundleNotices({
  repoRoot: fileURLToPath(new URL('..', import.meta.url)),
  uiRoot: fileURLToPath(new URL('.', import.meta.url)),
});

export default defineConfig({
  base: './',
  publicDir: false,
  plugins: [react(), notices.vite],
  // postcss.config.js's plugin, then the recorder of what each stylesheet inlined.
  css: { postcss: { plugins: [tailwindcss(), notices.postcss] } },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    emptyOutDir: true,
    outDir: fileURLToPath(new URL('../ios/Avibe/Resources/MobileApp', import.meta.url)),
    rollupOptions: {
      input: fileURLToPath(new URL('./mobile.html', import.meta.url)),
    },
  },
});

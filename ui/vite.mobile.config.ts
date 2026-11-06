import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  publicDir: false,
  plugins: [react()],
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

import { fileURLToPath, URL } from 'node:url';
import tailwindcss from '@tailwindcss/postcss';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Build with ui's locked React/toolchain dependencies. No second component library.
export default defineConfig({
  root: fileURLToPath(new URL('../desktop', import.meta.url)),
  base: './',
  publicDir: false,
  plugins: [react()],
  css: { postcss: { plugins: [tailwindcss({ base: fileURLToPath(new URL('.', import.meta.url)) })] } },
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    dedupe: ['react', 'react-dom'],
  },
  build: {
    target: ['es2022', 'safari15', 'chrome110'],
    outDir: 'dist',
    emptyOutDir: false,
    rolldownOptions: { input: fileURLToPath(new URL('../desktop/account/index.html', import.meta.url)) },
  },
});

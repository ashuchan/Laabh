import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';

export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
  ],
  resolve: {
    alias: {
      '@laabh/shared': path.resolve(__dirname, '../../packages/shared/src/index.ts'),
    },
  },
  server: {
    port: 5174,
    proxy: {
      '/portfolio': 'http://localhost:8000',
      '/signals': 'http://localhost:8000',
      '/reports': 'http://localhost:8000',
      '/fno': 'http://localhost:8000',
      '/trades': 'http://localhost:8000',
      '/watchlists': 'http://localhost:8000',
      '/analysts': 'http://localhost:8000',
      '/instruments': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});

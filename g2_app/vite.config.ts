import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  server: {
    host: 'localhost',
    port: 5173,
  },
  build: {
    sourcemap: false,
    outDir: 'dist',
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
      },
    },
  },
});

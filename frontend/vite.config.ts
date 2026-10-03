import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  plugins: [react()],
  server: {
    strictPort: true,
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  preview: {
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  build: { outDir: '../dist', emptyOutDir: true },
})

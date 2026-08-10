import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '~@fontsource/inter': '@fontsource/inter' } },
  server: { proxy: { '/mcp': 'http://localhost:30080' } },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 },
})

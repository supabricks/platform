import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/mcp': 'http://localhost:30080' } },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 },
})

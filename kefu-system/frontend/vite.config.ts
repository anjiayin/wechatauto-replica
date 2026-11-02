import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发代理 /api、/ws → 本机后端 8765；构建产物输出 dist（由 FastAPI 托管）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1200,
  },
})

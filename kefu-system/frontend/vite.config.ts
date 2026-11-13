import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端端口：默认 8765，可用环境变量 KEFU_PORT 覆盖（与 run.py 的
// 「命令行 > KEFU_PORT > 8765」口径对齐；这里读不到命令行参数，
// 只需保证改了端口时 dev 代理跟着走）。
const backendPort = process.env.KEFU_PORT || '8765'
const backendHost = process.env.KEFU_HOST || '127.0.0.1'
const httpTarget = `http://${backendHost}:${backendPort}`
const wsTarget = `ws://${backendHost}:${backendPort}`

// 开发代理 /api、/ws → 本机后端（默认 8765）；构建产物输出 dist（由 FastAPI 托管）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: httpTarget, changeOrigin: true },
      '/ws': { target: wsTarget, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1200,
  },
})

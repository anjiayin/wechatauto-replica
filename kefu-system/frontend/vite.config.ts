import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 版本号唯一真源：仓库根 version.txt。每次 dev / build 都硬校验三处一致，
// 不一致直接失败 —— 避免「打出来的包带着旧版本号」这种最难排查的事故。
// 注意：tsconfig.json 的 include 只有 src，vite.config.ts 不参与 tsc 类型检查。
const frontendDir = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(frontendDir, '../..')
const appVersion = readFileSync(path.join(repoRoot, 'version.txt'), 'utf8')
  .split(/\r?\n/)[0].trim()

const pkg = JSON.parse(readFileSync(path.join(frontendDir, 'package.json'), 'utf8'))
const lock = JSON.parse(readFileSync(path.join(frontendDir, 'package-lock.json'), 'utf8'))
const mismatches = [
  ['package.json', pkg.version],
  ['package-lock.json (root)', lock.version],
  ['package-lock.json (packages[""])', lock.packages?.['']?.version],
].filter(([, v]) => v !== appVersion)
if (mismatches.length) {
  throw new Error(
    `版本号不一致：version.txt=${appVersion}；` +
    mismatches.map(([f, v]) => `${f}=${v}`).join('、') +
    '。请运行 python kefu-system/tools/version_sync.py --sync',
  )
}

// 后端端口：默认 8765，可用环境变量 KEFU_PORT 覆盖（与 run.py 的
// 「命令行 > KEFU_PORT > 8765」口径对齐；这里读不到命令行参数，
// 只需保证改了端口时 dev 代理跟着走）。
const backendPort = process.env.KEFU_PORT || '8765'
const backendHost = process.env.KEFU_HOST || '127.0.0.1'
const httpTarget = `http://${backendHost}:${backendPort}`
const wsTarget = `ws://${backendHost}:${backendPort}`

// dist/version.json：给打包器（build_p1 / build_release）做机器可读的硬校验。
// 只有注入进 JS 的版本号不够——那需要解析压缩后的 bundle；有了这个文件，
// 「编了前端但没进包 / 改了 version.txt 忘了重新 build」会在打包时直接失败。
const emitVersionFile = {
  name: 'emit-version-file',
  generateBundle() {
    this.emitFile({
      type: 'asset',
      fileName: 'version.json',
      source: JSON.stringify({ version: appVersion }) + '\n',
    })
  },
}

// 开发代理 /api、/ws → 本机后端（默认 8765）；构建产物输出 dist（由 FastAPI 托管）
export default defineConfig({
  plugins: [react(), emitVersionFile],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
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

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发模式：Vite 5173 + uvicorn 8000，/api 走这里的代理。
// 生产模式：npm run build 出 dist/，由 FastAPI 用 StaticFiles 挂载成单端口。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})

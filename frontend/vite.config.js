import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发模式：Vite 51229 + uvicorn 8000，/api 走这里的代理。
// 生产模式：npm run build 出 dist/，由 FastAPI 用 StaticFiles 挂载成单端口。
export default defineConfig({
  plugins: [vue()],
  server: {
    // 监听所有网卡，局域网内其他机器可以直接用 <本机IP>:51229 打开。
    // 用 '0.0.0.0' 而不是 true —— 语义更明确，也避免 Vite 在某些环境下
    // 只绑 IPv6 通配地址导致 IPv4 访问不到。
    host: '0.0.0.0',
    port: 51229,
    proxy: {
      // 代理在 dev server 这一侧发起，所以这里指回本机后端即可 ——
      // 从别的机器访问时，请求由 Vite 转发，不需要客户端能直连 8000。
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

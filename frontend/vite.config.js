import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发服务器固定 5173（与后端默认允许的 CORS 来源一致）。
// API 地址不在这里配置：统一由 src/config.js 从 VITE_API_BASE_URL 读取。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
  },
  test: {
    // 前端测试保持最小化：只在 node 环境跑（组件用 react-dom/server 渲染，不引入 jsdom）
    environment: 'node',
    include: ['tests/**/*.test.{js,jsx}'],
  },
})

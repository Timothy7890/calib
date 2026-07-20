import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端 = run_server.py（默认 8132）
const BACKEND = 'http://localhost:8132'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 7012,
    proxy: {
      '/api': BACKEND,
    },
  },
})

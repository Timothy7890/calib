import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev server proxies API + WebSocket to the FastAPI backend (run_server.py).
// Change the target port if you start the backend on a different --port.
const BACKEND = 'http://localhost:8131'

export default defineConfig({
  plugins: [vue()],
  appType: 'spa',
  server: {
    port: 7011,
    proxy: {
      '/api': BACKEND,
      '/ws': {
        target: BACKEND.replace('http', 'ws'),
        ws: true,
      },
    },
  },
})

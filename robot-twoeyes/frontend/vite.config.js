import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  appType: 'spa',
  server: {
    port: 7009,
    historyApiFallback: true,
    proxy: {
      '/api': 'http://localhost:8124',
      '/calibrate/api': {
        target: 'http://localhost:8124',
      },
      '/calibrate/ws': {
        target: 'http://localhost:8124',
        ws: true,
      },
      '/ws': {
        target: 'ws://localhost:8124',
        ws: true,
      },
    },
  },
})

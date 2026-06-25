import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

function redirectRootPlugin() {
  return {
    name: 'redirect-root',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url === '/') {
          req.url = '/depth.html'
        }
        next()
      })
    },
  }
}

export default defineConfig({
  plugins: [vue(), redirectRootPlugin()],
  server: {
    port: 7009,
    proxy: {
      '/api': 'http://localhost:8124',
      '/ws': {
        target: 'ws://localhost:8124',
        ws: true,
      },
    },
  },
  build: {
    rollupOptions: {
      input: resolve(__dirname, 'depth.html'),
    },
  },
})

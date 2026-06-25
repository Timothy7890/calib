import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import RootApp from './RootApp.vue'
import './depth-global.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/depth' },
    {
      path: '/depth',
      component: () => import('./views/DepthCapture.vue'),
    },
    {
      path: '/calibrate',
      component: () => import('./views/CalibrationCapture.vue'),
    },
  ],
})

const app = createApp(RootApp)
app.use(router)
app.mount('#app')

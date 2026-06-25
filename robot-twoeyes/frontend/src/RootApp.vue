<template>
  <div class="root-app">
    <nav class="top-nav">
      <router-link to="/depth" class="nav-tab" active-class="active">深度采集</router-link>
      <router-link to="/calibrate" class="nav-tab" active-class="active">标定采集</router-link>
    </nav>
    <router-view />
  </div>
</template>

<style>
.top-nav {
  display: flex;
  justify-content: center;
  gap: 4px;
  padding: 12px 20px 0;
  max-width: 1400px;
  margin: 0 auto;
  background: #1a1a2e;
  border-bottom: 1px solid #2a2a4a;
  padding-bottom: 0;
}

.nav-tab {
  padding: 8px 28px;
  font-size: 0.95rem;
  font-weight: 500;
  color: #888;
  text-decoration: none;
  border-radius: 8px 8px 0 0;
  background: #12122a;
  border: 1px solid #2a2a4a;
  border-bottom: none;
  transition: all 0.15s;
  position: relative;
  top: 1px;
}

.nav-tab:hover {
  color: #ccc;
  background: #1a1a3a;
}

.nav-tab.active {
  color: #00d4ff;
  background: #1a1a2e;
  border-color: #00d4ff;
  border-bottom: 1px solid #1a1a2e;
}
</style>

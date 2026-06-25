<template>
  <div class="stream-view">
    <div class="camera-panel">
      <div class="camera-label">
        左相机
        <span :class="['dot', leftDetected ? 'detected' : 'not-detected']"></span>
      </div>
      <img v-if="leftSrc" :src="leftSrc" class="camera-img" alt="左相机" />
      <div v-else class="placeholder">等待画面...</div>
    </div>
    <div class="camera-panel">
      <div class="camera-label">
        右相机
        <span :class="['dot', rightDetected ? 'detected' : 'not-detected']"></span>
      </div>
      <img v-if="rightSrc" :src="rightSrc" class="camera-img" alt="右相机" />
      <div v-else class="placeholder">等待画面...</div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  leftSrc: { type: String, default: '' },
  rightSrc: { type: String, default: '' },
  leftDetected: { type: Boolean, default: false },
  rightDetected: { type: Boolean, default: false },
})
</script>

<style scoped>
.stream-view {
  display: flex;
  gap: 16px;
  width: 100%;
  justify-content: center;
  flex-wrap: wrap;
}

.camera-panel {
  flex: 1;
  max-width: 640px;
  min-width: 300px;
  background: #16213e;
  border-radius: 12px;
  overflow: hidden;
  border: 2px solid #2a2a4a;
}

.camera-label {
  padding: 8px 16px;
  font-size: 0.85rem;
  color: #aaa;
  display: flex;
  align-items: center;
  gap: 8px;
  background: #0f3460;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.dot.detected {
  background: #00e676;
  box-shadow: 0 0 6px #00e676;
}

.dot.not-detected {
  background: #ff5252;
  box-shadow: 0 0 6px #ff5252;
}

.camera-img {
  width: 100%;
  display: block;
}

.placeholder {
  height: 360px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #555;
  font-size: 1.1rem;
}
</style>

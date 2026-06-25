<template>
  <div class="history-panel" v-if="images.length > 0">
    <h3 class="panel-title">已拍摄图像 ({{ images.length }} 组)</h3>
    <div class="thumbnail-grid">
      <div
        v-for="img in images"
        :key="img"
        class="thumbnail-item"
        @click="$emit('preview', img)"
      >
        <img :src="`/api/images/left/${img}`" :alt="img" class="thumb-img" />
        <span class="thumb-label">{{ img }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  images: { type: Array, default: () => [] },
})

defineEmits(['preview'])
</script>

<style scoped>
.history-panel {
  margin-top: 24px;
  background: #16213e;
  border-radius: 12px;
  padding: 16px;
  border: 1px solid #2a2a4a;
}

.panel-title {
  font-size: 1rem;
  color: #aaa;
  margin-bottom: 12px;
}

.thumbnail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
}

.thumbnail-item {
  cursor: pointer;
  border-radius: 8px;
  overflow: hidden;
  background: #0f3460;
  transition: transform 0.15s, box-shadow 0.15s;
}

.thumbnail-item:hover {
  transform: scale(1.05);
  box-shadow: 0 4px 12px rgba(0, 212, 255, 0.3);
}

.thumb-img {
  width: 100%;
  display: block;
  aspect-ratio: 4/3;
  object-fit: cover;
}

.thumb-label {
  display: block;
  text-align: center;
  font-size: 0.75rem;
  color: #888;
  padding: 4px;
}
</style>

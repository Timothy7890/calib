<template>
  <div class="modal-overlay" @click.self="$emit('close')">
    <div class="modal-content">
      <div class="modal-header">
        <h3>预览: {{ filename }}</h3>
        <div class="modal-controls">
          <label class="corner-toggle">
            <input type="checkbox" v-model="showCorners" />
            显示角点
          </label>
          <button class="close-btn" @click="$emit('close')">✕</button>
        </div>
      </div>
      <div class="modal-body">
        <div class="preview-pair">
          <div class="preview-side">
            <div class="side-label">左相机</div>
            <img :src="leftUrl" alt="Left" class="preview-img" />
          </div>
          <div class="preview-side">
            <div class="side-label">右相机</div>
            <img :src="rightUrl" alt="Right" class="preview-img" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'

const props = defineProps({
  filename: { type: String, required: true },
  boardSize: { type: String, default: '9x6' },
})

defineEmits(['close'])

const showCorners = ref(false)

const leftUrl = computed(() => {
  const base = `/api/images/left/${props.filename}`
  return showCorners.value ? `${base}?corners=1&board_size=${props.boardSize}` : base
})

const rightUrl = computed(() => {
  const base = `/api/images/right/${props.filename}`
  return showCorners.value ? `${base}?corners=1&board_size=${props.boardSize}` : base
})
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 20px;
}

.modal-content {
  background: #1a1a2e;
  border-radius: 16px;
  width: 100%;
  max-width: 1200px;
  max-height: 90vh;
  overflow: auto;
  border: 1px solid #2a2a4a;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 24px;
  border-bottom: 1px solid #2a2a4a;
}

.modal-header h3 {
  color: #00d4ff;
  font-size: 1.1rem;
}

.modal-controls {
  display: flex;
  align-items: center;
  gap: 16px;
}

.corner-toggle {
  font-size: 0.85rem;
  color: #bbb;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
}

.corner-toggle input {
  accent-color: #00d4ff;
}

.close-btn {
  background: none;
  border: none;
  color: #888;
  font-size: 1.4rem;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
}

.close-btn:hover {
  color: #fff;
  background: #333;
}

.modal-body {
  padding: 24px;
}

.preview-pair {
  display: flex;
  gap: 16px;
  justify-content: center;
  flex-wrap: wrap;
}

.preview-side {
  flex: 1;
  min-width: 300px;
  max-width: 560px;
}

.side-label {
  font-size: 0.85rem;
  color: #aaa;
  margin-bottom: 8px;
  text-align: center;
}

.preview-img {
  width: 100%;
  border-radius: 8px;
  display: block;
}
</style>

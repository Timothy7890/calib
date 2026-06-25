<template>
  <button class="capture-btn" :disabled="disabled || capturing" @click="capture">
    <span v-if="capturing">保存中...</span>
    <span v-else-if="disabled">等待角点检测...</span>
    <span v-else>拍照</span>
  </button>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['captured'])
const capturing = ref(false)

async function capture() {
  capturing.value = true
  try {
    const res = await fetch('/api/capture', { method: 'POST' })
    const data = await res.json()
    if (data.success) {
      emit('captured', data)
    } else {
      alert('拍照失败: ' + (data.error || '未知错误'))
    }
  } catch (e) {
    alert('请求失败: ' + e.message)
  } finally {
    capturing.value = false
  }
}
</script>

<style scoped>
.capture-btn {
  padding: 14px 48px;
  font-size: 1.1rem;
  font-weight: 600;
  border: none;
  border-radius: 50px;
  background: linear-gradient(135deg, #00d4ff, #0077b6);
  color: #fff;
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 4px 15px rgba(0, 212, 255, 0.3);
}

.capture-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(0, 212, 255, 0.5);
}

.capture-btn:active:not(:disabled) {
  transform: translateY(0);
}

.capture-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>

<template>
  <div class="app">
    <header class="header">
      <h1>双目标定 · 图像采集</h1>
      <div class="status-bar">
        <span class="status-item">
          已拍摄: <strong>{{ captureCount }}</strong> 组
        </span>
        <span class="status-item">
          棋盘格:
          <input
            v-model="boardSizeInput"
            class="board-input"
            @keyup.enter="updateBoardSize"
            @blur="updateBoardSize"
          />
        </span>
        <label class="status-item toggle">
          <input type="checkbox" v-model="showCorners" />
          显示角点
        </label>
        <span class="status-item save-path" v-if="savePath">
          保存: <strong>{{ savePath }}</strong>
        </span>
      </div>
    </header>

    <main class="main-content">
      <StreamView
        :left-src="leftSrc"
        :right-src="rightSrc"
        :left-detected="leftDetected"
        :right-detected="rightDetected"
      />
      <div class="capture-area">
        <CaptureButton :disabled="!canCapture" @captured="onCaptured" />
        <label class="status-item toggle force-toggle">
          <input type="checkbox" v-model="allowNoCorners" />
          允许无角点拍摄
        </label>
      </div>
    </main>

    <HistoryPanel
      :images="historyImages"
      @preview="openPreview"
    />

    <PreviewModal
      v-if="previewFile"
      :filename="previewFile"
      :board-size="boardSizeInput"
      @close="previewFile = null"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import StreamView from './components/StreamView.vue'
import CaptureButton from './components/CaptureButton.vue'
import HistoryPanel from './components/HistoryPanel.vue'
import PreviewModal from './components/PreviewModal.vue'

const leftSrc = ref('')
const rightSrc = ref('')
const leftDetected = ref(false)
const rightDetected = ref(false)
const captureCount = ref(0)
const showCorners = ref(true)
const boardSizeInput = ref('9x6')
const historyImages = ref([])
const previewFile = ref(null)
const savePath = ref('')
const allowNoCorners = ref(false)

const canCapture = computed(() => {
  if (allowNoCorners.value) return true
  return leftDetected.value && rightDetected.value
})

let ws = null

function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${protocol}//${location.host}/ws/stream`)

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    leftSrc.value = 'data:image/jpeg;base64,' + data.left
    rightSrc.value = 'data:image/jpeg;base64,' + data.right
    leftDetected.value = data.left_detected
    rightDetected.value = data.right_detected
    captureCount.value = data.count
  }

  ws.onclose = () => {
    setTimeout(connectWebSocket, 2000)
  }

  ws.onerror = () => {
    ws.close()
  }
}

function sendWsCommand(cmd) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(cmd))
  }
}

watch(showCorners, (val) => {
  sendWsCommand({ show_corners: val })
})

function updateBoardSize() {
  sendWsCommand({ board_size: boardSizeInput.value })
  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ board_size: boardSizeInput.value }),
  })
}

async function loadHistory() {
  try {
    const res = await fetch('/api/history')
    const data = await res.json()
    historyImages.value = data.images || []
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

function onCaptured() {
  loadHistory()
}

function openPreview(filename) {
  previewFile.value = filename
}

async function loadStatus() {
  try {
    const res = await fetch('/api/status')
    const data = await res.json()
    if (data.board_size) boardSizeInput.value = data.board_size
    if (data.count !== undefined) captureCount.value = data.count
    if (data.save_path) savePath.value = data.save_path
  } catch (e) {
    console.error('Failed to load status:', e)
  }
}

onMounted(() => {
  loadStatus()
  connectWebSocket()
  loadHistory()
})

onUnmounted(() => {
  if (ws) ws.close()
})
</script>

<style>
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #1a1a2e;
  color: #eee;
  min-height: 100vh;
}

.app {
  max-width: 1400px;
  margin: 0 auto;
  padding: 20px;
}

.header {
  text-align: center;
  margin-bottom: 24px;
}

.header h1 {
  font-size: 1.6rem;
  font-weight: 600;
  color: #00d4ff;
  margin-bottom: 12px;
}

.status-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 24px;
  flex-wrap: wrap;
}

.status-item {
  font-size: 0.9rem;
  color: #bbb;
}

.status-item strong {
  color: #00d4ff;
}

.board-input {
  width: 60px;
  padding: 2px 6px;
  border: 1px solid #555;
  border-radius: 4px;
  background: #2a2a4a;
  color: #eee;
  font-size: 0.9rem;
  text-align: center;
}

.toggle {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
}

.toggle input[type="checkbox"] {
  accent-color: #00d4ff;
}

.main-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  margin-bottom: 24px;
}

.capture-area {
  display: flex;
  align-items: center;
  gap: 16px;
}

.force-toggle {
  font-size: 0.8rem;
  color: #888;
}
</style>

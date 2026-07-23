<template>
  <div class="calib-app">
    <header class="header">
      <h1>双目标定 · 图像采集</h1>
      <div class="status-bar">
        <span class="status-item">
          已拍摄: <strong>{{ captureCount }}</strong> 组
        </span>
        <span class="status-item">
          分辨率: <strong>{{ resolution || '获取中...' }}</strong><template v-if="resolution"> / 眼</template>
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
      <div class="image-pair">
        <div class="panel">
          <div class="panel-label">
            左眼
            <span class="detect-dot" :class="{ detected: leftDetected }"></span>
          </div>
          <div class="image-container">
            <img v-if="leftSrc" :src="leftSrc" class="camera-img" alt="左眼" />
            <div v-else class="placeholder">等待画面...</div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-label">
            右眼
            <span class="detect-dot" :class="{ detected: rightDetected }"></span>
          </div>
          <div class="image-container">
            <img v-if="rightSrc" :src="rightSrc" class="camera-img" alt="右眼" />
            <div v-else class="placeholder">等待画面...</div>
          </div>
        </div>
      </div>

      <div class="capture-area">
        <button class="capture-btn" :class="{ running: autoRunning }" :disabled="captureBtnDisabled" @click="onCaptureBtn">
          {{ captureBtnLabel }}
        </button>
        <label class="status-item toggle force-toggle">
          <input type="checkbox" v-model="allowNoCorners" />
          允许无角点拍摄
        </label>
      </div>

      <div class="auto-area">
        <label class="status-item toggle auto-toggle">
          <input type="checkbox" v-model="autoCapture" />
          自动连拍
        </label>
        <span class="status-item">
          首次等待
          <input
            type="number"
            min="1"
            max="120"
            step="1"
            v-model.number="prepSec"
            class="interval-input"
            :disabled="autoRunning"
          />
          秒
        </span>
        <span class="status-item">
          间隔
          <input
            type="number"
            min="1"
            max="120"
            step="1"
            v-model.number="intervalSec"
            class="interval-input"
            :disabled="autoRunning"
          />
          秒
        </span>
        <span v-if="autoCapture && !autoRunning" class="status-item auto-hint">
          点击「开始自动连拍」启动
        </span>
      </div>

      <div v-if="autoRunning" class="countdown-banner" :class="{ waiting: !canCapture }">
        <div class="countdown-big">{{ countdown }}</div>
        <div class="countdown-status">
          <span class="countdown-title">{{ inPrepPhase ? '准备中…' : '自动连拍中' }}</span>
          <span class="countdown-sub">{{ canCapture ? '秒后自动拍摄' : '等待左右眼检测到角点…' }}</span>
          <span v-if="lastAutoMsg" class="auto-msg">{{ lastAutoMsg }}</span>
        </div>
      </div>
    </main>

    <!-- History -->
    <div class="history" v-if="historyImages.length > 0">
      <h3>拍摄历史 ({{ historyImages.length }})</h3>
      <div class="history-grid">
        <div
          v-for="filename in historyImages"
          :key="filename"
          class="history-item"
        >
          <img :src="`/calibrate/api/images/left/${filename}`" class="thumb" />
          <span class="thumb-label">{{ filename }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'

const API_PREFIX = '/calibrate'

const leftSrc = ref('')
const rightSrc = ref('')
const leftDetected = ref(false)
const rightDetected = ref(false)
const captureCount = ref(0)
const showCorners = ref(true)
const boardSizeInput = ref('11x8')
const historyImages = ref([])
const savePath = ref('')
const resolution = ref('')
const allowNoCorners = ref(false)
const capturing = ref(false)

const autoCapture = ref(false)
const autoRunning = ref(false)
const inPrepPhase = ref(false)
const prepSec = ref(10)
const intervalSec = ref(3)
const countdown = ref(0)
const lastAutoMsg = ref('')

const canCapture = computed(() => {
  if (allowNoCorners.value) return true
  return leftDetected.value && rightDetected.value
})

const captureBtnLabel = computed(() => {
  if (!autoCapture.value) return capturing.value ? '拍摄中...' : '拍摄'
  if (autoRunning.value) return '停止自动连拍'
  return '开始自动连拍'
})

const captureBtnDisabled = computed(() => {
  if (autoCapture.value) return false
  return !canCapture.value || capturing.value
})

let ws = null
let countdownTimer = null

function clampSec(v) {
  const n = Math.round(Number(v) || 0)
  return Math.min(120, Math.max(1, n))
}

function stopTimer() {
  if (countdownTimer) clearInterval(countdownTimer)
  countdownTimer = null
}

function tick() {
  countdown.value -= 1
  if (countdown.value > 0) return
  inPrepPhase.value = false
  countdown.value = clampSec(intervalSec.value)
  if (capturing.value) return
  if (canCapture.value) {
    captureFrame()
    lastAutoMsg.value = ''
  } else {
    lastAutoMsg.value = '未检测到角点，已跳过本轮'
  }
}

function startAuto() {
  if (autoRunning.value) return
  stopTimer()
  autoRunning.value = true
  inPrepPhase.value = true
  countdown.value = clampSec(prepSec.value)
  lastAutoMsg.value = ''
  countdownTimer = setInterval(tick, 1000)
}

function stopAuto() {
  stopTimer()
  autoRunning.value = false
  inPrepPhase.value = false
  countdown.value = 0
}

function onCaptureBtn() {
  if (!autoCapture.value) {
    captureFrame()
    return
  }
  if (autoRunning.value) stopAuto()
  else startAuto()
}

watch(autoCapture, (on) => {
  if (!on) stopAuto()
})

function onKeydown(e) {
  if (e.code !== 'Space') return
  // 输入框聚焦时不拦截空格
  const tag = (e.target && e.target.tagName) || ''
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
  e.preventDefault() // 防止页面滚动/触发聚焦按钮的默认点击
  if (captureBtnDisabled.value) return
  onCaptureBtn()
}

function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${protocol}//${location.host}${API_PREFIX}/ws/stream`)

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    leftSrc.value = 'data:image/jpeg;base64,' + data.left
    rightSrc.value = 'data:image/jpeg;base64,' + data.right
    leftDetected.value = data.left_detected
    rightDetected.value = data.right_detected
    captureCount.value = data.count
    if (data.resolution) resolution.value = data.resolution
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
  fetch(`${API_PREFIX}/api/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ board_size: boardSizeInput.value }),
  })
}

async function captureFrame() {
  capturing.value = true
  try {
    const res = await fetch(`${API_PREFIX}/api/capture`, { method: 'POST' })
    const data = await res.json()
    if (data.success) {
      captureCount.value = data.count
      loadHistory()
    } else {
      alert('拍摄失败: ' + (data.error || '未知错误'))
    }
  } catch (e) {
    alert('请求失败: ' + e.message)
  } finally {
    capturing.value = false
  }
}

async function loadHistory() {
  try {
    const res = await fetch(`${API_PREFIX}/api/history`)
    const data = await res.json()
    historyImages.value = data.images || []
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

async function loadStatus() {
  try {
    const res = await fetch(`${API_PREFIX}/api/status`)
    const data = await res.json()
    if (data.board_size) boardSizeInput.value = data.board_size
    if (data.count !== undefined) captureCount.value = data.count
    if (data.save_path) savePath.value = data.save_path
    if (data.resolution) resolution.value = data.resolution
  } catch (e) {
    console.error('Failed to load status:', e)
  }
}

onMounted(() => {
  loadStatus()
  connectWebSocket()
  loadHistory()
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  stopAuto()
  if (ws) ws.close()
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.calib-app {
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

.image-pair {
  display: flex;
  gap: 16px;
  width: 100%;
  justify-content: center;
  flex-wrap: wrap;
}

.panel {
  flex: 1;
  max-width: 640px;
  min-width: 300px;
  background: #16213e;
  border-radius: 12px;
  overflow: hidden;
  border: 2px solid #2a2a4a;
}

.panel-label {
  padding: 8px 16px;
  font-size: 0.85rem;
  color: #aaa;
  background: #0f3460;
  display: flex;
  align-items: center;
  gap: 8px;
}

.detect-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #555;
  transition: background 0.2s;
}

.detect-dot.detected {
  background: #00ff88;
  box-shadow: 0 0 6px rgba(0, 255, 136, 0.6);
}

.image-container {
  position: relative;
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

.capture-area {
  display: flex;
  align-items: center;
  gap: 16px;
}

.capture-btn {
  padding: 14px 48px;
  font-size: 1.1rem;
  font-weight: 600;
  border: none;
  border-radius: 50px;
  background: linear-gradient(135deg, #ff6b35, #f7c948);
  color: #fff;
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 4px 15px rgba(255, 107, 53, 0.3);
}

.capture-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(255, 107, 53, 0.5);
}

.capture-btn:active:not(:disabled) {
  transform: translateY(0);
}

.capture-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.capture-btn.running {
  background: linear-gradient(135deg, #e53935, #b71c1c);
  box-shadow: 0 4px 15px rgba(229, 57, 53, 0.4);
}

.auto-hint {
  color: #00d4ff;
}

.force-toggle {
  font-size: 0.8rem;
  color: #888;
}

.auto-area {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 20px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.auto-toggle {
  font-size: 0.95rem;
  color: #00d4ff;
}

.interval-input {
  width: 56px;
  padding: 2px 6px;
  border: 1px solid #555;
  border-radius: 4px;
  background: #2a2a4a;
  color: #eee;
  font-size: 0.9rem;
  text-align: center;
}

.interval-input:disabled {
  opacity: 0.6;
}

.countdown-banner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 28px;
  margin-top: 12px;
  padding: 20px 48px;
  border-radius: 18px;
  background: rgba(0, 212, 255, 0.12);
  border: 2px solid rgba(0, 212, 255, 0.5);
  box-shadow: 0 0 24px rgba(0, 212, 255, 0.25);
}

.countdown-banner.waiting {
  background: rgba(255, 107, 53, 0.12);
  border-color: rgba(255, 107, 53, 0.55);
  box-shadow: 0 0 24px rgba(255, 107, 53, 0.25);
}

.countdown-big {
  font-size: 7rem;
  font-weight: 800;
  color: #00d4ff;
  line-height: 1;
  min-width: 1.6ch;
  text-align: center;
  font-variant-numeric: tabular-nums;
  text-shadow: 0 0 20px rgba(0, 212, 255, 0.5);
}

.countdown-banner.waiting .countdown-big {
  color: #ff6b35;
  text-shadow: 0 0 20px rgba(255, 107, 53, 0.5);
}

.countdown-status {
  display: flex;
  flex-direction: column;
  gap: 6px;
  text-align: left;
}

.countdown-title {
  font-size: 1.5rem;
  font-weight: 700;
  color: #eee;
}

.countdown-sub {
  font-size: 1rem;
  color: #aaa;
}

.auto-msg {
  font-size: 0.9rem;
  color: #ff9b6b;
}

.save-path {
  font-size: 0.8rem;
}

/* History */
.history {
  margin-top: 24px;
  background: #16213e;
  border-radius: 12px;
  padding: 16px;
  border: 1px solid #2a2a4a;
}

.history h3 {
  font-size: 1rem;
  color: #aaa;
  margin-bottom: 12px;
}

.history-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
}

.history-item {
  border-radius: 8px;
  overflow: hidden;
  background: #0f3460;
}

.thumb {
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

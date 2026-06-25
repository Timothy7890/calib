<template>
  <div class="depth-app">
    <header class="header">
      <h1>深度图采集 · 点云生成</h1>
      <div class="status-bar">
        <span class="status-item">已采集: <strong>{{ captureCount }}</strong> 组</span>
        <label class="status-item toggle">
          <input type="checkbox" v-model="wlsEnabled" @change="onWlsToggle" />
          WLS 滤波
        </label>
        <label class="status-item method-select">
          视差算法:
          <select v-model="disparityMethod" @change="onMethodChange">
            <option value="sgbm">SGBM</option>
            <option value="crestereo">CREStereo</option>
          </select>
        </label>
        <span class="status-item save-path" v-if="savePath">保存: <strong>{{ savePath }}</strong></span>
      </div>
    </header>

    <main class="main-content">
      <div class="image-pair">
        <!-- Left: RGB preview (clickable, with canvas overlay) -->
        <div class="panel">
          <div class="panel-label">左眼 RGB</div>
          <div class="image-container" ref="leftContainer">
            <img
              v-if="leftSrc"
              :src="leftSrc"
              class="camera-img"
              alt="左眼"
              @click="onImageClick"
              @load="onLeftImgLoad"
              ref="leftImg"
            />
            <div v-else class="placeholder">等待画面...</div>
            <canvas
              ref="overlayCanvas"
              class="overlay-canvas"
            ></canvas>
          </div>
        </div>

        <!-- Right column: depth + YOLO panel -->
        <div class="right-column">
          <!-- Depth visualization -->
          <div class="panel">
            <div class="panel-label">
              深度图
              <span v-if="depthValue !== null" class="depth-value">
                点击深度: {{ depthValue.toFixed(1) }} mm
              </span>
            </div>
            <div class="image-container">
              <img
                v-if="depthSrc"
                :src="depthSrc"
                class="camera-img"
                alt="深度图"
              />
              <div v-else class="placeholder depth-placeholder">未采集</div>
            </div>
          </div>

          <!-- YOLO Detection Result Panel -->
          <div class="panel yolo-panel" v-if="yoloResult">
            <div class="panel-label yolo-label">YOLO 检测结果</div>
            <div class="yolo-content">
              <div v-for="(det, idx) in yoloResult.detections" :key="idx" class="yolo-detection">
                <div class="yolo-row">
                  <span class="yolo-key">类别</span>
                  <span class="yolo-val">{{ det.class_name }}</span>
                  <span class="yolo-confidence">{{ (det.confidence * 100).toFixed(1) }}%</span>
                </div>
                <div class="yolo-row" v-if="det.corners">
                  <span class="yolo-key">角点</span>
                  <div class="yolo-extremes">
                    <span>左上({{ det.corners.tl[0] }}, {{ det.corners.tl[1] }})</span>
                    <span>右上({{ det.corners.tr[0] }}, {{ det.corners.tr[1] }})</span>
                    <span>左下({{ det.corners.bl[0] }}, {{ det.corners.bl[1] }})</span>
                    <span>右下({{ det.corners.br[0] }}, {{ det.corners.br[1] }})</span>
                  </div>
                </div>
                <div class="yolo-row" v-if="det.center">
                  <span class="yolo-key">中心点</span>
                  <span class="yolo-val">({{ det.center[0] }}, {{ det.center[1] }})</span>
                </div>
                <div class="yolo-row" v-if="det.centerDepth !== undefined && det.centerDepth !== null">
                  <span class="yolo-key">中心深度</span>
                  <span class="yolo-val yolo-depth">{{ det.centerDepth.toFixed(1) }} mm</span>
                </div>
              </div>
              <div v-if="yoloResult.detections.length === 0" class="yolo-empty">
                未检测到目标
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="controls">
        <button class="capture-btn" :disabled="capturing" @click="capture">
          <span v-if="capturing">计算中...</span>
          <span v-else>采集 & 生成深度</span>
        </button>
        <span v-if="lastNumPoints" class="point-count">
          点云: {{ lastNumPoints.toLocaleString() }} 点
        </span>
      </div>
    </main>

    <!-- History -->
    <div class="history" v-if="history.length > 0">
      <h3>采集历史 ({{ history.length }})</h3>
      <div class="history-grid">
        <div
          v-for="idx in history"
          :key="idx"
          class="history-item"
          @click="loadCapture(idx)"
        >
          <img :src="`/api/images/depth_viz_${idx}.jpg`" class="thumb" />
          <span class="thumb-label">#{{ idx }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'

const YOLO_PORT = 8125

const leftSrc = ref('')
const depthSrc = ref('')
const captureCount = ref(0)
const savePath = ref('')
const capturing = ref(false)
const wlsEnabled = ref(true)
const disparityMethod = ref('sgbm')
const clickPoint = ref(null)
const depthValue = ref(null)
const lastNumPoints = ref(null)
const history = ref([])
const currentIndex = ref(-1)
const yoloResult = ref(null)
const capturedLeftB64 = ref('')

const leftContainer = ref(null)
const leftImg = ref(null)
const overlayCanvas = ref(null)

let ws = null

function getYoloUrl(path) {
  return `http://${window.location.hostname}:${YOLO_PORT}${path}`
}

function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${protocol}//${location.host}/ws/stream`)

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    leftSrc.value = 'data:image/jpeg;base64,' + data.left
  }

  ws.onclose = () => {
    setTimeout(connectWebSocket, 2000)
  }

  ws.onerror = () => {
    ws.close()
  }
}

// --- Capture flow with YOLO integration ---

async function capture() {
  capturing.value = true
  clickPoint.value = null
  depthValue.value = null
  yoloResult.value = null
  clearCanvas()

  try {
    const res = await fetch('/api/capture', { method: 'POST' })
    const data = await res.json()
    if (!data.success) {
      alert('采集失败: ' + (data.error || '未知错误'))
      return
    }

    depthSrc.value = 'data:image/jpeg;base64,' + data.depth_viz
    captureCount.value = data.count
    currentIndex.value = data.index
    lastNumPoints.value = data.num_points
    capturedLeftB64.value = data.left_image
    leftSrc.value = 'data:image/jpeg;base64,' + data.left_image
    loadHistory()

    await runYoloDetection(data.left_image, data.index)
  } catch (e) {
    alert('请求失败: ' + e.message)
  } finally {
    capturing.value = false
  }
}

async function runYoloDetection(leftB64, captureIndex) {
  try {
    const blob = b64ToBlob(leftB64, 'image/jpeg')
    const formData = new FormData()
    formData.append('file', blob, 'capture.jpg')

    const res = await fetch(getYoloUrl('/api/segment'), {
      method: 'POST',
      body: formData,
    })
    const data = await res.json()

    const detections = data.detections || []
    for (const det of detections) {
      const contour = det.contour || []
      if (contour.length > 0) {
        det.corners = computeMaskCorners(contour)
        det.center = [
          Math.round((det.corners.tl[0] + det.corners.br[0]) / 2),
          Math.round((det.corners.tl[1] + det.corners.br[1]) / 2),
        ]

        try {
          const depthRes = await fetch(`/api/depth_at?index=${captureIndex}&x=${det.center[0]}&y=${det.center[1]}`)
          const depthData = await depthRes.json()
          det.centerDepth = depthData.depth_mm ?? null
        } catch {
          det.centerDepth = null
        }
      } else {
        det.corners = null
        det.center = null
        det.centerDepth = null
      }
    }

    yoloResult.value = { detections, count: data.count }
    await nextTick()
    drawOverlay()
  } catch (e) {
    console.error('YOLO detection failed:', e)
  }
}

function computeMaskCorners(contour) {
  let tl = contour[0], tr = contour[0], bl = contour[0], br = contour[0]
  let tlVal = Infinity, trVal = -Infinity, blVal = -Infinity, brVal = -Infinity
  for (const pt of contour) {
    const [x, y] = pt
    const sum = x + y
    const diff = x - y
    if (sum < tlVal) { tlVal = sum; tl = pt }        // 左上: x+y 最小
    if (diff > trVal) { trVal = diff; tr = pt }       // 右上: x-y 最大
    if (-diff > blVal) { blVal = -diff; bl = pt }     // 左下: y-x 最大
    if (sum > brVal) { brVal = sum; br = pt }          // 右下: x+y 最大
  }
  return { tl, tr, bl, br }
}

function b64ToBlob(b64, mime) {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return new Blob([bytes], { type: mime })
}

// --- Canvas drawing ---

function clearCanvas() {
  const canvas = overlayCanvas.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  ctx.clearRect(0, 0, canvas.width, canvas.height)
}

function onLeftImgLoad() {
  resizeCanvas()
}

function resizeCanvas() {
  const img = leftImg.value
  const canvas = overlayCanvas.value
  if (!img || !canvas) return
  canvas.width = img.clientWidth
  canvas.height = img.clientHeight
  drawOverlay()
}

function imgToCanvas(imgX, imgY) {
  const img = leftImg.value
  if (!img) return { x: 0, y: 0 }
  const scaleX = img.clientWidth / img.naturalWidth
  const scaleY = img.clientHeight / img.naturalHeight
  return { x: imgX * scaleX, y: imgY * scaleY }
}

function drawOverlay() {
  const canvas = overlayCanvas.value
  const img = leftImg.value
  if (!canvas || !img) return
  const ctx = canvas.getContext('2d')
  ctx.clearRect(0, 0, canvas.width, canvas.height)

  // Draw YOLO detections
  if (yoloResult.value) {
    for (const det of yoloResult.value.detections) {
      const contour = det.contour || []

      // Draw contour polygon
      if (contour.length > 2) {
        ctx.beginPath()
        const start = imgToCanvas(contour[0][0], contour[0][1])
        ctx.moveTo(start.x, start.y)
        for (let i = 1; i < contour.length; i++) {
          const p = imgToCanvas(contour[i][0], contour[i][1])
          ctx.lineTo(p.x, p.y)
        }
        ctx.closePath()
        ctx.fillStyle = 'rgba(0, 255, 100, 0.15)'
        ctx.fill()
        ctx.strokeStyle = 'rgba(0, 255, 100, 0.8)'
        ctx.lineWidth = 2
        ctx.stroke()
      }

      // Draw bbox corner points
      if (det.corners) {
        const labels = { tl: '左上', tr: '右上', bl: '左下', br: '右下' }
        const offsets = {
          tl: [-30, -8], tr: [7, -8],
          bl: [-30, 14], br: [7, 14],
        }
        for (const [key, label] of Object.entries(labels)) {
          const pt = det.corners[key]
          const p = imgToCanvas(pt[0], pt[1])
          ctx.beginPath()
          ctx.arc(p.x, p.y, 5, 0, Math.PI * 2)
          ctx.fillStyle = '#ff4444'
          ctx.fill()
          ctx.strokeStyle = '#fff'
          ctx.lineWidth = 1.5
          ctx.stroke()
          ctx.fillStyle = '#fff'
          ctx.font = 'bold 11px sans-serif'
          const off = offsets[key]
          ctx.fillText(label, p.x + off[0], p.y + off[1])
        }
      }

      // Draw center point
      if (det.center) {
        const p = imgToCanvas(det.center[0], det.center[1])
        const armLen = 10

        ctx.beginPath()
        ctx.moveTo(p.x - armLen, p.y)
        ctx.lineTo(p.x + armLen, p.y)
        ctx.moveTo(p.x, p.y - armLen)
        ctx.lineTo(p.x, p.y + armLen)
        ctx.strokeStyle = '#ffdd00'
        ctx.lineWidth = 2.5
        ctx.stroke()

        ctx.beginPath()
        ctx.arc(p.x, p.y, 4, 0, Math.PI * 2)
        ctx.fillStyle = '#ffdd00'
        ctx.fill()

        if (det.centerDepth !== null && det.centerDepth !== undefined) {
          ctx.fillStyle = '#ffdd00'
          ctx.font = 'bold 12px sans-serif'
          ctx.fillText(`${det.centerDepth.toFixed(1)}mm`, p.x + 12, p.y - 8)
        }
      }
    }
  }

  // Draw manual click point
  if (clickPoint.value) {
    const p = imgToCanvas(clickPoint.value.imgX, clickPoint.value.imgY)
    ctx.beginPath()
    ctx.arc(p.x, p.y, 10, 0, Math.PI * 2)
    ctx.strokeStyle = '#00aaff'
    ctx.lineWidth = 3
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(p.x, p.y, 2, 0, Math.PI * 2)
    ctx.fillStyle = '#00aaff'
    ctx.fill()

    if (depthValue.value !== null) {
      ctx.fillStyle = '#00aaff'
      ctx.font = 'bold 12px sans-serif'
      ctx.fillText(`${depthValue.value.toFixed(1)}mm`, p.x + 12, p.y - 8)
    }
  }
}

// --- Click-to-depth ---

function onImageClick(event) {
  if (currentIndex.value < 0) return
  const img = leftImg.value
  if (!img) return

  const rect = img.getBoundingClientRect()
  const scaleX = img.naturalWidth / rect.width
  const scaleY = img.naturalHeight / rect.height

  const px = event.clientX - rect.left
  const py = event.clientY - rect.top
  const imgX = Math.round(px * scaleX)
  const imgY = Math.round(py * scaleY)

  clickPoint.value = { px, py, imgX, imgY }
  queryDepth(currentIndex.value, imgX, imgY)
}

async function queryDepth(index, x, y) {
  try {
    const res = await fetch(`/api/depth_at?index=${index}&x=${x}&y=${y}`)
    const data = await res.json()
    if (data.depth_mm !== undefined) {
      depthValue.value = data.depth_mm
      await nextTick()
      drawOverlay()
    }
  } catch (e) {
    console.error('Depth query failed:', e)
  }
}

// --- History ---

async function loadHistory() {
  try {
    const res = await fetch('/api/history')
    const data = await res.json()
    history.value = data.captures || []
  } catch (e) {
    console.error('Failed to load history:', e)
  }
}

function loadCapture(idx) {
  depthSrc.value = `/api/images/depth_viz_${idx}.jpg`
  currentIndex.value = parseInt(idx)
  clickPoint.value = null
  depthValue.value = null
  yoloResult.value = null
  clearCanvas()
}

async function loadStatus() {
  try {
    const res = await fetch('/api/status')
    const data = await res.json()
    if (data.count !== undefined) captureCount.value = data.count
    if (data.save_path) savePath.value = data.save_path
    if (data.use_wls !== undefined) wlsEnabled.value = data.use_wls
    if (data.disparity_method) disparityMethod.value = data.disparity_method
  } catch (e) {
    console.error('Failed to load status:', e)
  }
}

async function onMethodChange() {
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disparity_method: disparityMethod.value }),
    })
  } catch (e) {
    console.error('Failed to update disparity method:', e)
  }
}

async function onWlsToggle() {
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ use_wls: wlsEnabled.value }),
    })
  } catch (e) {
    console.error('Failed to update WLS config:', e)
  }
}

// --- Resize observer ---

let resizeObserver = null

onMounted(() => {
  loadStatus()
  connectWebSocket()
  loadHistory()

  resizeObserver = new ResizeObserver(() => { resizeCanvas() })
  if (leftContainer.value) resizeObserver.observe(leftContainer.value)
})

onUnmounted(() => {
  if (ws) ws.close()
  if (resizeObserver) resizeObserver.disconnect()
})
</script>

<style scoped>
.depth-app {
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

.toggle {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
}

.toggle input[type="checkbox"] {
  accent-color: #00d4ff;
}

.method-select {
  display: flex;
  align-items: center;
  gap: 6px;
}

.method-select select {
  background: #0f3460;
  color: #00d4ff;
  border: 1px solid #2a2a4a;
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 0.85rem;
  cursor: pointer;
}

.nav-link {
  color: #00d4ff;
  text-decoration: none;
}

.nav-link:hover {
  text-decoration: underline;
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
  align-items: flex-start;
  flex-wrap: wrap;
}

.right-column {
  flex: 1;
  max-width: 640px;
  min-width: 300px;
  display: flex;
  flex-direction: column;
  gap: 16px;
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

.right-column .panel {
  flex: none;
}

.panel-label {
  padding: 8px 16px;
  font-size: 0.85rem;
  color: #aaa;
  background: #0f3460;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.depth-value {
  color: #00aaff;
  font-weight: 600;
  font-size: 0.9rem;
}

.image-container {
  position: relative;
  cursor: crosshair;
}

.camera-img {
  width: 100%;
  display: block;
}

.overlay-canvas {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.placeholder {
  height: 360px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #555;
  font-size: 1.1rem;
}

.depth-placeholder {
  background: #fff;
  color: #999;
}

/* YOLO panel */
.yolo-label {
  background: #1a3a2e;
  color: #66ffaa;
}

.yolo-content {
  padding: 12px 16px;
}

.yolo-detection {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.yolo-detection + .yolo-detection {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #2a2a4a;
}

.yolo-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 0.85rem;
}

.yolo-key {
  color: #888;
  min-width: 56px;
  flex-shrink: 0;
}

.yolo-val {
  color: #ddd;
}

.yolo-confidence {
  color: #66ffaa;
  font-weight: 600;
  margin-left: auto;
}

.yolo-depth {
  color: #ffdd00;
  font-weight: 700;
  font-size: 1rem;
}

.yolo-extremes {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  color: #ff8888;
  font-size: 0.8rem;
}

.yolo-empty {
  color: #666;
  font-size: 0.85rem;
  text-align: center;
  padding: 8px;
}

/* Controls */
.controls {
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

.point-count {
  font-size: 0.9rem;
  color: #aaa;
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
  cursor: pointer;
  border-radius: 8px;
  overflow: hidden;
  background: #0f3460;
  transition: transform 0.15s, box-shadow 0.15s;
}

.history-item:hover {
  transform: scale(1.05);
  box-shadow: 0 4px 12px rgba(255, 107, 53, 0.3);
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

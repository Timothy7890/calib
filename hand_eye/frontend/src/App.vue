<template>
  <div class="app">
    <header class="header">
      <h1>手眼标定 · 数据采集</h1>
      <div class="status-bar">
        <span class="status-item">已采集: <strong>{{ captureCount }}</strong> 组</span>
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
        <span class="status-item">相机: <strong>{{ cameraSource }}</strong></span>
        <span class="status-item">关节源: <strong>{{ jointSource }}</strong></span>
        <span class="status-item save-path" v-if="savePath">保存: <strong>{{ savePath }}</strong></span>
      </div>
    </header>

    <main class="main-content">
      <div class="panel eye-panel">
        <div class="panel-label">
          左眼
          <span class="detect-dot" :class="{ detected: leftDetected }"></span>
          <span class="detect-text">{{ leftDetected ? '检测到棋盘格' : '未检测到' }}</span>
        </div>
        <div class="image-container">
          <img v-if="leftSrc" :src="leftSrc" class="camera-img" alt="左眼" />
          <div v-else class="placeholder">等待画面...</div>
        </div>
      </div>

      <div class="panel eye-panel">
        <div class="panel-label">
          右眼
          <span class="detect-dot" :class="{ detected: rightDetected }"></span>
          <span class="detect-text">{{ rightDetected ? '检测到棋盘格' : '未检测到' }}</span>
        </div>
        <div class="image-container">
          <img v-if="rightSrc" :src="rightSrc" class="camera-img" alt="右眼" />
          <div v-else class="placeholder">等待画面...</div>
        </div>
      </div>

      <div class="panel arm-panel" v-if="armAvailable">
        <div class="arm-header">
          <h3>右臂点动控制</h3>
          <span class="arm-state" :class="{ on: armEngaged }">
            {{ armEngaged ? '已接管(持位中)' : '未接管' }}
          </span>
          <span class="arm-state" :class="{ on: armJog }">
            {{ armJog ? 'jog 已使能' : 'jog 锁定' }}
          </span>
          <span class="arm-state warn" v-if="armFloat">手调松力中</span>
          <div class="arm-buttons">
            <button v-if="!armJog" class="arm-btn enable" @click="enableArm">
              {{ armFloat ? '使能 jog(接住)' : '使能 jog' }}
            </button>
            <button v-else class="arm-btn disable" @click="disableArm">关闭 jog(保持)</button>
            <button v-if="!armJog && !armFloat" class="arm-btn handmove" @click="handMoveArm">进入手调(松力)</button>
            <button class="arm-btn estop" @click="stopArm">急停(冻结)</button>
          </div>
        </div>
        <div v-if="armFloat" class="float-warning">
          ⚠ 右臂已松力(手调中)— 无重力补偿,请<strong>全程用手托住手臂</strong>!松手会塌下。
          搬到大致位置后点 <strong>"使能 jog(接住)"</strong> 刚性接管,再用 0.2° 精调。
        </div>
        <div class="arm-steprow">
          <span class="arm-steplabel">微调步距:</span>
          <button
            v-for="s in nudgeSteps" :key="s"
            class="step-btn" :class="{ active: nudgeStep === s }"
            @click="nudgeStep = s"
          >{{ s }}°</button>
        </div>
        <p class="arm-note">限速 {{ maxSpeedRad.toFixed(2) }} rad/s · 拖动慢速跟随 · 自动夹紧关节限位 · 关闭服务前请托住手臂</p>
        <table class="arm-table">
          <thead>
            <tr><th>关节</th><th>目标 (°)</th><th class="slider-col">调节</th><th>实测 (°)</th><th>微调</th></tr>
          </thead>
          <tbody>
            <tr v-for="(name, i) in armNames" :key="name">
              <td class="jzh">{{ zhName(name) }}</td>
              <td class="jval">
                <input
                  type="number" step="0.5" class="deg-input"
                  :disabled="!armJog"
                  v-model.number="desiredDeg[i]"
                  @change="onDesiredChange(i)"
                />
              </td>
              <td class="slider-col">
                <input
                  type="range" step="0.5" class="deg-slider"
                  :disabled="!armJog"
                  :min="limitsDeg[i] ? limitsDeg[i][0] : -180"
                  :max="limitsDeg[i] ? limitsDeg[i][1] : 180"
                  v-model.number="desiredDeg[i]"
                  @input="onDesiredChange(i)"
                />
              </td>
              <td class="jdeg measured">{{ measuredDeg[i] !== undefined ? measuredDeg[i].toFixed(1) : '—' }}</td>
              <td class="nudge-cell">
                <button class="nudge-btn" :disabled="!armJog" @click="nudge(i, -nudgeStep)">−{{ nudgeStep }}°</button>
                <button class="nudge-btn" :disabled="!armJog" @click="nudge(i, nudgeStep)">＋{{ nudgeStep }}°</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </main>

    <div class="capture-area">
      <button class="capture-btn" :disabled="!canCapture || capturing" @click="captureFrame">
        <span v-if="capturing">采集中...</span>
        <span v-else-if="!jointsOk">等待关节数据...</span>
        <span v-else-if="!leftDetected && !allowNoCorners">等待棋盘格...</span>
        <span v-else>采集 (图像 + 关节)</span>
      </button>
      <label class="status-item toggle force-toggle">
        <input type="checkbox" v-model="allowNoCorners" />
        允许无角点采集
      </label>
      <span v-if="lastCapture" class="last-capture">
        已存 #{{ lastCapture.index }}: q = [{{ lastCapture.q_rad.map(fmt).join(', ') }}]
      </span>
    </div>

    <div class="tcp-panel">
      <div class="tcp-header">
        <h3>TCP 标定采集 (Pivot 四点法)</h3>
        <span class="tcp-count">已记录 <strong>{{ tcpItems.length }}</strong> 个姿态</span>
      </div>
      <p class="tcp-note">
        让工具尖怼住空间中同一个固定参照点,换 ≥4 个差异大的姿态,每次点"记录关节"。
        只存关节角,不存图像。标接近轴时:针尖用"tip"组,针上第二点用"p2"组,两组都怼同一个点。
      </p>
      <div class="tcp-controls">
        <label class="tcp-group-label">
          组:
          <select v-model="tcpGroup" class="tcp-select">
            <option value="tip">针尖 (tip) · TCP 点</option>
            <option value="p2">第二点 (p2) · 定接近轴</option>
          </select>
        </label>
        <button
          class="tcp-btn"
          :disabled="!jointsOk || tcpCapturing"
          @click="captureTcp"
        >
          <span v-if="tcpCapturing">记录中...</span>
          <span v-else-if="!jointsOk">等待关节数据...</span>
          <span v-else>记录关节 → {{ tcpGroup }}</span>
        </button>
        <span class="tcp-groupcounts">
          <span v-for="(n, g) in tcpGroups" :key="g" class="tcp-chip">{{ g }}: {{ n }}</span>
        </span>
      </div>
      <div class="tcp-list" v-if="tcpItems.length > 0">
        <div v-for="it in tcpItems" :key="it.index" class="tcp-item">
          <span class="tcp-item-g" :class="it.group">{{ it.group }}</span>
          <span class="tcp-item-idx">#{{ String(it.index).padStart(4, '0') }}</span>
          <span class="tcp-item-time">{{ (it.datetime || '').replace('T', ' ') }}</span>
          <button class="tcp-del" @click="deleteTcp(it.index)">删除</button>
        </div>
      </div>
    </div>

    <div class="history" v-if="historyImages.length > 0">
      <h3>采集历史 ({{ historyImages.length }})</h3>
      <div class="history-grid">
        <div v-for="filename in historyImages" :key="filename" class="history-item">
          <img :src="`/api/images/left/${filename}`" class="thumb" />
          <span class="thumb-label">{{ filename }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'

const leftSrc = ref('')
const leftDetected = ref(false)
const rightSrc = ref('')
const rightDetected = ref(false)
const captureCount = ref(0)
const showCorners = ref(true)
const boardSizeInput = ref('11x8')
const historyImages = ref([])
const savePath = ref('')
const cameraSource = ref('')
const jointSource = ref('')
const allowNoCorners = ref(false)
const capturing = ref(false)
const lastCapture = ref(null)

const jointNames = ref([])
const jointsQ = ref([])
const jointsOk = ref(false)
const jointsError = ref('')

// --- right-arm jog control state ---
const armAvailable = ref(false)
const armEngaged = ref(false)
const armJog = ref(false)
const armFloat = ref(false)
const armNames = ref([])
const desiredDeg = ref([])
const measuredDeg = ref([])
const limitsDeg = ref([])
const maxSpeedRad = ref(0.2)
const nudgeStep = ref(1)
const nudgeSteps = [1, 0.5, 0.2]

// --- TCP pivot capture state ---
const tcpGroup = ref('tip')
const tcpItems = ref([])
const tcpGroups = ref({})
const tcpCapturing = ref(false)

const R2D = 180 / Math.PI
const D2R = Math.PI / 180

const canCapture = computed(() => {
  if (!jointsOk.value) return false
  if (allowNoCorners.value) return true
  return leftDetected.value
})

function fmt(v) {
  return Number(v).toFixed(4)
}
function fmtDeg(v) {
  return `${(Number(v) * 180 / Math.PI).toFixed(1)}°`
}
const ZH_TOKENS = {
  left: '左',
  right: '右',
  shoulder: '肩',
  elbow: '肘',
  wrist: '腕',
  pitch: '俯仰',
  roll: '横滚',
  yaw: '偏航',
  arm: '臂',
}
function zhName(name) {
  return name
    .replace(/_joint$/, '')
    .split('_')
    .map((tok) => ZH_TOKENS[tok] || tok)
    .join('')
}

let ws = null
function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${protocol}//${location.host}/ws/stream`)
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    leftSrc.value = 'data:image/jpeg;base64,' + data.left
    leftDetected.value = data.left_detected
    if (data.right !== undefined) {
      rightSrc.value = 'data:image/jpeg;base64,' + data.right
      rightDetected.value = data.right_detected
    }
    captureCount.value = data.count
  }
  ws.onclose = () => setTimeout(connectWebSocket, 2000)
  ws.onerror = () => ws.close()
}

function sendWsCommand(cmd) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(cmd))
}

watch(showCorners, (val) => sendWsCommand({ show_corners: val }))

function updateBoardSize() {
  sendWsCommand({ board_size: boardSizeInput.value })
  fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ board_size: boardSizeInput.value }),
  })
}

async function captureFrame() {
  capturing.value = true
  try {
    const res = await fetch('/api/capture', { method: 'POST' })
    const data = await res.json()
    if (data.success) {
      captureCount.value = data.count
      lastCapture.value = { index: data.index, q_rad: data.q_rad }
      loadHistory()
    } else {
      alert('采集失败: ' + (data.error || '未知错误'))
    }
  } catch (e) {
    alert('请求失败: ' + e.message)
  } finally {
    capturing.value = false
  }
}

async function loadHistory() {
  try {
    const res = await fetch('/api/history')
    const data = await res.json()
    historyImages.value = data.images || []
  } catch (e) {
    console.error('history failed', e)
  }
}

async function loadStatus() {
  try {
    const res = await fetch('/api/status')
    const data = await res.json()
    if (data.board_size) boardSizeInput.value = data.board_size
    if (data.count !== undefined) captureCount.value = data.count
    if (data.save_path) savePath.value = data.save_path
    if (data.camera_source) cameraSource.value = data.camera_source
    if (data.joint_source) jointSource.value = data.joint_source
    if (data.joint_names) jointNames.value = data.joint_names
  } catch (e) {
    console.error('status failed', e)
  }
}

let jointsTimer = null
async function pollJoints() {
  try {
    const res = await fetch('/api/joints')
    const data = await res.json()
    if (data.ok) {
      jointsOk.value = true
      jointsError.value = ''
      jointNames.value = data.joint_names
      jointsQ.value = data.q
    } else {
      jointsOk.value = false
      jointsError.value = data.error || '读取失败'
    }
  } catch (e) {
    jointsOk.value = false
    jointsError.value = e.message
  }
}

function clampDeg(i, val) {
  const lim = limitsDeg.value[i]
  if (!lim) return val
  return Math.min(lim[1], Math.max(lim[0], val))
}

async function sendDesired() {
  const q = desiredDeg.value.map((d) => Number(d) * D2R)
  try {
    await fetch('/api/arm/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q }),
    })
  } catch (e) {
    console.error('arm set failed', e)
  }
}

function onDesiredChange(i) {
  desiredDeg.value[i] = clampDeg(i, Number(desiredDeg.value[i]))
  sendDesired()
}

function nudge(i, ddeg) {
  desiredDeg.value[i] = clampDeg(i, Number(desiredDeg.value[i] || 0) + ddeg)
  sendDesired()
}

async function enableArm() {
  await fetch('/api/arm/enable', { method: 'POST' })
  await pollArm(true)
}
async function disableArm() {
  await fetch('/api/arm/disable', { method: 'POST' })
  await pollArm(true)
}
async function stopArm() {
  await fetch('/api/arm/stop', { method: 'POST' })
  await pollArm(true)
}
async function handMoveArm() {
  const res = await fetch('/api/arm/handmove', { method: 'POST' })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    alert('无法进入手调: ' + (data.error || '请先关闭 jog'))
  }
  await pollArm(true)
}

let armTimer = null
async function pollArm(syncDesired = false) {
  try {
    const res = await fetch('/api/arm/status')
    const data = await res.json()
    if (!data.available) {
      armAvailable.value = false
      return
    }
    armAvailable.value = true
    armEngaged.value = data.engaged
    armJog.value = data.jog_enabled
    armFloat.value = !!data.float
    armNames.value = data.joint_names || []
    maxSpeedRad.value = data.max_speed_rad_s || 0.2
    if (data.limits_rad) {
      limitsDeg.value = data.limits_rad.map((p) => [p[0] * R2D, p[1] * R2D])
    }
    if (data.measured_rad) {
      measuredDeg.value = data.measured_rad.map((v) => v * R2D)
    }
    // Sync the sliders from the server only when jog is locked (server owns
    // the target) or on an explicit sync; otherwise the user owns the sliders.
    if ((syncDesired || !data.jog_enabled) && data.desired_rad) {
      desiredDeg.value = data.desired_rad.map((v) => +(v * R2D).toFixed(1))
    } else if (desiredDeg.value.length === 0 && data.desired_rad) {
      desiredDeg.value = data.desired_rad.map((v) => +(v * R2D).toFixed(1))
    }
  } catch (e) {
    armAvailable.value = false
  }
}

async function loadTcp() {
  try {
    const res = await fetch('/api/tcp/list')
    const data = await res.json()
    tcpItems.value = data.items || []
    tcpGroups.value = data.groups || {}
  } catch (e) {
    console.error('tcp list failed', e)
  }
}

async function captureTcp() {
  tcpCapturing.value = true
  try {
    const res = await fetch('/api/tcp/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group: tcpGroup.value }),
    })
    const data = await res.json()
    if (data.success) {
      await loadTcp()
    } else {
      alert('记录失败: ' + (data.error || '未知错误'))
    }
  } catch (e) {
    alert('请求失败: ' + e.message)
  } finally {
    tcpCapturing.value = false
  }
}

async function deleteTcp(index) {
  try {
    await fetch(`/api/tcp/${index}`, { method: 'DELETE' })
    await loadTcp()
  } catch (e) {
    console.error('tcp delete failed', e)
  }
}

onMounted(() => {
  loadStatus()
  connectWebSocket()
  loadHistory()
  pollJoints()
  jointsTimer = setInterval(pollJoints, 500)
  pollArm(true)
  armTimer = setInterval(pollArm, 400)
  loadTcp()
})

onUnmounted(() => {
  if (ws) ws.close()
  if (jointsTimer) clearInterval(jointsTimer)
  if (armTimer) clearInterval(armTimer)
})
</script>

<style scoped>
.app {
  max-width: 1700px;
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
  gap: 20px;
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
  text-align: center;
}
.toggle {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
}
.toggle input[type='checkbox'] {
  accent-color: #00d4ff;
}
.main-content {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: center;
  flex-wrap: wrap;
  margin-bottom: 24px;
}
.panel {
  background: #16213e;
  border-radius: 12px;
  overflow: hidden;
  border: 2px solid #2a2a4a;
  display: flex;
  flex-direction: column;
}
.eye-panel {
  flex: 1 1 0;
  min-width: 300px;
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
.detect-text {
  margin-left: auto;
  font-size: 0.8rem;
}
.detect-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #ff5252;
  box-shadow: 0 0 6px #ff5252;
  transition: background 0.2s;
}
.detect-dot.detected {
  background: #00e676;
  box-shadow: 0 0 6px #00e676;
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
.jzh {
  color: #ddd;
  font-size: 0.9rem;
}
.jval {
  text-align: right;
  color: #00d4ff;
  font-weight: 600;
}
.jdeg {
  text-align: right;
  color: #888;
  font-size: 0.85rem;
  width: 70px;
}
.capture-area {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 24px;
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
.capture-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.force-toggle {
  font-size: 0.8rem;
  color: #888;
}
.last-capture {
  font-size: 0.8rem;
  color: #00e676;
  font-variant-numeric: tabular-nums;
}
.arm-panel {
  background: #16213e;
  border-radius: 12px;
  padding: 14px 18px;
  border: 1px solid #2a2a4a;
  flex: 0 1 500px;
  min-width: 460px;
  align-self: flex-start;
}
.arm-header {
  display: flex;
  align-items: center;
  gap: 8px 10px;
  flex-wrap: wrap;
}
.arm-header h3 {
  font-size: 1rem;
  color: #00d4ff;
  margin: 0;
}
.arm-state {
  font-size: 0.8rem;
  padding: 2px 10px;
  border-radius: 20px;
  background: #2a2a4a;
  color: #888;
}
.arm-state.on {
  background: rgba(0, 230, 118, 0.18);
  color: #00e676;
}
.arm-state.warn {
  background: rgba(255, 152, 0, 0.22);
  color: #ffb74d;
}
.arm-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.arm-btn {
  padding: 6px 13px;
  border: none;
  border-radius: 8px;
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
  color: #fff;
}
.arm-btn.enable { background: #00b377; }
.arm-btn.disable { background: #3a6ea5; }
.arm-btn.estop { background: #e53935; }
.arm-btn.handmove { background: #b9770a; }
.float-warning {
  margin-top: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: rgba(255, 152, 0, 0.14);
  border: 1px solid rgba(255, 152, 0, 0.5);
  color: #ffcc80;
  font-size: 0.9rem;
  line-height: 1.5;
}
.float-warning strong { color: #ffb74d; }
.arm-steprow {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.arm-steplabel {
  font-size: 0.85rem;
  color: #aaa;
}
.step-btn {
  padding: 4px 14px;
  border: 1px solid #3a6ea5;
  border-radius: 6px;
  background: #1a2a44;
  color: #9cc4ff;
  cursor: pointer;
  font-size: 0.85rem;
}
.step-btn.active {
  background: #00d4ff;
  color: #06243a;
  font-weight: 600;
  border-color: #00d4ff;
}
.arm-note {
  font-size: 0.8rem;
  color: #ff9b6b;
  margin: 10px 0 6px;
}
.arm-table {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
}
.arm-table th {
  text-align: left;
  font-size: 0.78rem;
  color: #888;
  font-weight: 500;
  padding: 5px 6px;
  border-bottom: 1px solid #2a2a4a;
}
.arm-table td {
  padding: 5px 6px;
  border-bottom: 1px solid #22223a;
}
.arm-table .jzh {
  white-space: nowrap;
  width: 100%;
}
.slider-col { width: 150px; }
.deg-slider {
  width: 150px;
  max-width: 100%;
  accent-color: #00d4ff;
  vertical-align: middle;
}
.arm-table .jval,
.arm-table .measured,
.arm-table .nudge-cell { white-space: nowrap; }
.deg-input {
  width: 62px;
  padding: 4px 6px;
  border: 1px solid #555;
  border-radius: 4px;
  background: #2a2a4a;
  color: #00d4ff;
  text-align: right;
}
.deg-input:disabled, .deg-slider:disabled { opacity: 0.5; }
.measured { color: #aaa; }
.nudge-cell { white-space: nowrap; }
.nudge-btn {
  padding: 4px 8px;
  margin: 0 2px;
  border: 1px solid #3a6ea5;
  border-radius: 4px;
  background: #1a2a44;
  color: #9cc4ff;
  cursor: pointer;
}
.nudge-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.tcp-panel {
  background: #16213e;
  border-radius: 12px;
  padding: 16px 20px;
  border: 1px solid #2a2a4a;
  margin-bottom: 24px;
}
.tcp-header {
  display: flex;
  align-items: center;
  gap: 16px;
}
.tcp-header h3 {
  font-size: 1.05rem;
  color: #00d4ff;
  margin: 0;
}
.tcp-count {
  font-size: 0.85rem;
  color: #888;
}
.tcp-count strong {
  color: #00d4ff;
}
.tcp-note {
  font-size: 0.8rem;
  color: #9aa4c0;
  margin: 10px 0;
  line-height: 1.5;
}
.tcp-controls {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.tcp-group-label {
  font-size: 0.9rem;
  color: #bbb;
}
.tcp-select {
  padding: 6px 10px;
  border: 1px solid #555;
  border-radius: 6px;
  background: #2a2a4a;
  color: #eee;
  margin-left: 6px;
}
.tcp-btn {
  padding: 10px 28px;
  font-size: 1rem;
  font-weight: 600;
  border: none;
  border-radius: 40px;
  background: linear-gradient(135deg, #3a8dde, #00d4ff);
  color: #fff;
  cursor: pointer;
}
.tcp-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.tcp-groupcounts {
  display: flex;
  gap: 8px;
}
.tcp-chip {
  font-size: 0.8rem;
  padding: 3px 10px;
  border-radius: 20px;
  background: #2a2a4a;
  color: #9cc4ff;
}
.tcp-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 220px;
  overflow-y: auto;
}
.tcp-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 10px;
  border-bottom: 1px solid #22223a;
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
}
.tcp-item-g {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  background: #2a2a4a;
  color: #aaa;
}
.tcp-item-g.tip { background: rgba(0, 212, 255, 0.18); color: #00d4ff; }
.tcp-item-g.p2 { background: rgba(255, 155, 107, 0.18); color: #ff9b6b; }
.tcp-item-idx { color: #ddd; }
.tcp-item-time { color: #777; margin-right: auto; }
.tcp-del {
  padding: 3px 10px;
  border: 1px solid #6a3a3a;
  border-radius: 4px;
  background: #2a1a1a;
  color: #ff9c9c;
  cursor: pointer;
}

.history {
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

<script setup>
import { computed, onMounted, ref } from 'vue'

const status = ref(null)
const samples = ref([])
const result = ref(null)
const errorMsg = ref('')

// 当前待配对的一组数据
const pick = ref(null)        // {p_camera, depth_mm, pixel, valid_ratio}
const pickBusy = ref(false)
const wristT = ref(null)      // 自动读取到的 4x4
const wristManual = ref({ x: '', y: '', z: '', roll: '', pitch: '', yaw: '' })
const clickPos = ref(null)

const imgEl = ref(null)

async function refreshStatus() {
  status.value = await (await fetch('/api/status')).json()
}

async function refreshSamples() {
  const data = await (await fetch('/api/samples')).json()
  samples.value = data.samples
}

// ---- 视频点击 → 反投影 ----

async function onVideoClick(ev) {
  const img = imgEl.value
  if (!img || !status.value?.camera?.width) return
  const rect = img.getBoundingClientRect()
  const relX = (ev.clientX - rect.left) / rect.width
  const relY = (ev.clientY - rect.top) / rect.height
  const u = Math.round(relX * status.value.camera.width)
  const v = Math.round(relY * status.value.camera.height)
  clickPos.value = { xPct: relX * 100, yPct: relY * 100 }

  pickBusy.value = true
  errorMsg.value = ''
  pick.value = null
  try {
    const res = await fetch('/api/pick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ u, v }),
    })
    const data = await res.json()
    if (data.ok) {
      pick.value = data
      // 自动位姿源：点击取点的同时立刻抓一次手腕位姿，保证时间对齐
      if (autoPose.value) await readWristPose()
    } else {
      errorMsg.value = data.error || '取点失败'
    }
  } catch (e) {
    errorMsg.value = String(e)
  } finally {
    pickBusy.value = false
  }
}

// ---- 手腕位姿 ----

const autoPose = computed(() => status.value?.pose_auto)

async function readWristPose() {
  errorMsg.value = ''
  try {
    const res = await fetch('/api/wrist_pose')
    const data = await res.json()
    if (data.ok) {
      wristT.value = data.T_base_wrist
    } else {
      errorMsg.value = data.error
    }
  } catch (e) {
    errorMsg.value = String(e)
  }
}

const manualValid = computed(() =>
  Object.values(wristManual.value).every((s) => s !== '' && isFinite(Number(s))),
)

const wristReady = computed(() => (autoPose.value ? !!wristT.value : manualValid.value))
const canSave = computed(() => pick.value && wristReady.value)

// ---- 保存样本 ----

async function saveSample() {
  if (!canSave.value) return
  errorMsg.value = ''
  const body = { p_camera: pick.value.p_camera, pixel: pick.value.pixel }
  if (autoPose.value && wristT.value) {
    body.T_base_wrist = wristT.value
  } else {
    body.wrist_xyz = [Number(wristManual.value.x), Number(wristManual.value.y), Number(wristManual.value.z)]
    body.wrist_rpy = [Number(wristManual.value.roll), Number(wristManual.value.pitch), Number(wristManual.value.yaw)]
  }
  const res = await fetch('/api/samples', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json()
  if (!data.ok) {
    errorMsg.value = data.error || '保存失败'
    return
  }
  pick.value = null
  clickPos.value = null
  wristT.value = null
  result.value = null
  await refreshSamples()
}

async function deleteSample(index) {
  await fetch(`/api/samples/${index}`, { method: 'DELETE' })
  result.value = null
  await refreshSamples()
}

// ---- 解算 ----

const solveBusy = ref(false)
const minSamples = computed(() => status.value?.min_samples ?? 5)

async function solve() {
  solveBusy.value = true
  errorMsg.value = ''
  try {
    const res = await fetch('/api/solve', { method: 'POST' })
    const data = await res.json()
    if (data.ok) {
      result.value = data
    } else {
      errorMsg.value = data.error || '解算失败'
    }
  } finally {
    solveBusy.value = false
  }
}

const matrixText = computed(() => {
  if (!result.value) return ''
  return result.value.T_cam2base
    .map((r) => r.map((v) => v.toFixed(5).padStart(9)).join('  '))
    .join('\n')
})

function fmt(v, d = 4) { return Number(v).toFixed(d) }
function wristSummary(T) {
  return `[${T[0][3].toFixed(3)}, ${T[1][3].toFixed(3)}, ${T[2][3].toFixed(3)}]`
}

// ---- 手臂点动（--arm-control 时后端才有） ----

const arm = ref(null)             // /api/arm/status 的返回
const armBusy = ref(false)
const stepDeg = ref(2)            // 步长（度）
const stepOptions = [0.5, 1, 2, 5, 10]

async function refreshArm() {
  try {
    arm.value = await (await fetch('/api/arm/status')).json()
  } catch { /* 后端未起或断连，下轮再试 */ }
}

async function armPost(path, body) {
  armBusy.value = true
  errorMsg.value = ''
  try {
    const res = await fetch(`/api/arm/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    })
    const data = await res.json()
    if (!data.ok) errorMsg.value = data.error || `${path} 失败`
    else arm.value = { enabled: true, ...data }
  } catch (e) {
    errorMsg.value = String(e)
  } finally {
    armBusy.value = false
  }
}

function nudgeJoint(index, sign) {
  const delta = sign * stepDeg.value * Math.PI / 180
  armPost('nudge', { index, delta })
}

function handMove() {
  if (!confirm('卸力后手臂会因重力下坠，请先扶住手臂！确认进入卸力模式？')) return
  armPost('hand_move')
}

function jointShortName(name) {
  return name.replace(/^(left|right)_/, '').replace(/_joint$/, '')
}

onMounted(async () => {
  await refreshStatus()
  await refreshSamples()
  await refreshArm()
  setInterval(refreshArm, 800)
})
</script>

<template>
  <header class="topbar">
    <h1>Hand-Eye 3D 标定</h1>
    <span class="sub">
      眼在手外 · 联合估计指尖偏移 · 输出 T_{{ status?.base_link || 'base' }}←camera（彩色相机系）
    </span>
    <div class="spacer" />
    <span v-if="status" class="badge">
      相机: {{ status.camera.name || status.camera.source }} {{ status.camera.serial }}
    </span>
    <span v-if="status?.camera?.width" class="badge">
      分辨率: {{ status.camera.width }}×{{ status.camera.height }}
    </span>
    <span v-if="status" class="badge">
      位姿源: {{ status.pose_source }} ({{ status.wrist_link }})
    </span>
    <span v-if="status" class="badge">样本: {{ samples.length }}</span>
  </header>

  <div class="layout">
    <!-- 左：视频 -->
    <div class="video-panel">
      <div class="video-wrap">
        <img ref="imgEl" :src="'/api/stream'" @click="onVideoClick" />
        <div
          v-if="clickPos"
          class="crosshair"
          :style="{ left: clickPos.xPct + '%', top: clickPos.yPct + '%' }"
        />
      </div>
      <div class="video-hint">
        机械臂停稳后，点击画面中的标记点（指尖/手背贴纸中心）。后端做多帧中值滤波并反投影；
        自动位姿源会在点击的同一时刻抓取手腕位姿。避免点物体边缘（深度飞点）。
        <b>手腕的朝向也要在各样本间充分变化</b>，否则指尖偏移解不出来。
      </div>
    </div>

    <!-- 右：操作区 -->
    <div class="side-panel">
      <!-- 手臂点动（--arm-control 时显示） -->
      <div v-if="arm?.enabled" class="card">
        <h2>
          0. {{ arm.arm === 'right' ? '右' : '左' }}臂点动
          <span class="badge" :class="arm.float ? 'bad' : (arm.jog_enabled ? 'good' : '')">
            {{ arm.float ? '卸力中（扶住！）' : (arm.jog_enabled ? '点动开启' : '刚性保持') }}
          </span>
        </h2>
        <div class="field-row">
          <label>模式</label>
          <button v-if="!arm.jog_enabled && !arm.float" class="btn primary"
                  :disabled="armBusy" @click="armPost('enable_jog')">开启点动</button>
          <button v-if="arm.jog_enabled" class="btn"
                  :disabled="armBusy" @click="armPost('disable_jog')">停止点动</button>
          <button v-if="!arm.jog_enabled && !arm.float" class="btn warn"
                  :disabled="armBusy" @click="handMove">卸力拖动</button>
          <button v-if="arm.float" class="btn primary"
                  :disabled="armBusy" @click="armPost('stop')">保持当前位置</button>
        </div>
        <div class="field-row">
          <label>步长</label>
          <span class="step-group">
            <button v-for="s in stepOptions" :key="s" class="btn step-btn"
                    :class="{ active: stepDeg === s }" @click="stepDeg = s">{{ s }}°</button>
          </span>
        </div>
        <table class="jog-table">
          <tbody>
            <tr v-for="(name, i) in arm.joint_names" :key="name">
              <td class="jog-name">{{ jointShortName(name) }}</td>
              <td class="jog-val">
                {{ arm.measured_rad ? (arm.measured_rad[i] * 180 / Math.PI).toFixed(1) : '?' }}°
              </td>
              <td>
                <button class="btn jog-btn" :disabled="!arm.jog_enabled || armBusy"
                        @click="nudgeJoint(i, -1)">−</button>
                <button class="btn jog-btn" :disabled="!arm.jog_enabled || armBusy"
                        @click="nudgeJoint(i, +1)">+</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="video-hint">
          点动有限速（{{ arm.max_speed_rad_s }} rad/s）并钳制在关节限位内。
          卸力模式下 kp=0 只留阻尼，手臂会下坠，务必有人扶住；
          摆好位置后点「保持当前位置」即锁定。
        </div>
      </div>

      <!-- 当前样本 -->
      <div class="card">
        <h2>1. 当前样本</h2>
        <div class="field-row">
          <label>P_camera</label>
          <span v-if="pickBusy" class="coord dim">取点中…</span>
          <span v-else-if="pick" class="coord ok">
            [{{ fmt(pick.p_camera[0]) }}, {{ fmt(pick.p_camera[1]) }}, {{ fmt(pick.p_camera[2]) }}] m
            · 深度 {{ Math.round(pick.depth_mm) }}mm
          </span>
          <span v-else class="coord dim">← 在左侧画面上点击标记点</span>
        </div>

        <template v-if="autoPose">
          <div class="field-row">
            <label>手腕位姿</label>
            <span v-if="wristT" class="coord ok">t = {{ wristSummary(wristT) }} m（自动）</span>
            <span v-else class="coord dim">点击取点时自动抓取</span>
            <button class="btn" @click="readWristPose">重读</button>
          </div>
        </template>
        <template v-else>
          <div class="field-row">
            <label>腕 xyz (m)</label>
            <input v-model="wristManual.x" placeholder="x" />
            <input v-model="wristManual.y" placeholder="y" />
            <input v-model="wristManual.z" placeholder="z" />
          </div>
          <div class="field-row">
            <label>腕 rpy (rad)</label>
            <input v-model="wristManual.roll" placeholder="roll" />
            <input v-model="wristManual.pitch" placeholder="pitch" />
            <input v-model="wristManual.yaw" placeholder="yaw" />
          </div>
        </template>

        <div class="field-row">
          <label></label>
          <button class="btn primary" :disabled="!canSave" @click="saveSample">保存这个样本</button>
        </div>
        <div v-if="errorMsg" class="err-text">⚠ {{ errorMsg }}</div>
      </div>

      <!-- 样本列表 -->
      <div class="card">
        <h2>2. 已采样本（{{ samples.length }} / 最少 {{ minSamples }}，建议 ≥ 12）</h2>
        <table v-if="samples.length">
          <thead>
            <tr>
              <th>#</th><th>P_camera (m)</th><th>腕 t (m)</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in samples" :key="s.index">
              <td>{{ s.index }}</td>
              <td>{{ s.p_camera.map((v) => fmt(v, 3)).join(', ') }}</td>
              <td>{{ wristSummary(s.T_base_wrist) }}</td>
              <td><button class="del-btn" title="删除" @click="deleteSample(s.index)">✕</button></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="coord dim">还没有样本</div>
      </div>

      <!-- 解算 -->
      <div class="card">
        <h2>3. 解算 T_base←camera + 指尖偏移</h2>
        <button class="btn primary" :disabled="samples.length < minSamples || solveBusy" @click="solve">
          {{ solveBusy ? '解算中…' : `用 ${samples.length} 个样本解算` }}
        </button>
        <template v-if="result">
          <div style="margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;">
            <span class="badge" :class="result.residual_mm.rms < 8 ? 'good' : 'bad'">
              拟合 RMS {{ fmt(result.residual_mm.rms, 2) }} mm
            </span>
            <span v-if="result.leave_one_out_stats_mm" class="badge"
                  :class="result.leave_one_out_stats_mm.mean < 10 ? 'good' : 'bad'">
              留一验证均值 {{ fmt(result.leave_one_out_stats_mm.mean, 2) }} mm
            </span>
            <span class="badge">
              p_tool(腕系) [{{ result.p_tool_wrist_m.map((v) => fmt(v, 3)).join(', ') }}] m
            </span>
            <span class="badge">
              rpy(deg) [{{ result.rpy_deg.map((v) => fmt(v, 2)).join(', ') }}]
            </span>
            <span class="badge">腕姿态跨度 {{ fmt(result.wrist_rotation_spread_deg, 1) }}°</span>
          </div>
          <div class="result-box" style="margin-top: 10px;">{{ matrixText }}</div>
          <div class="video-hint">已保存到 {{ result.saved_to }}</div>
        </template>
      </div>
    </div>
  </div>
</template>

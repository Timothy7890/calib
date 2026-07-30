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

// 固定相机外参、只解 p_tool（点击指尖尖端的样本）
const toolResult = ref(null)
const toolBusy = ref(false)

async function solveToolOnly() {
  toolBusy.value = true
  errorMsg.value = ''
  try {
    const res = await fetch('/api/solve_tool', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    const data = await res.json()
    if (data.ok) toolResult.value = data
    else errorMsg.value = data.error || '解算失败'
  } finally {
    toolBusy.value = false
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
  if (!confirm('卸力拖动：重力前馈会让手臂近似失重（推到哪停哪），但补偿有偏差时仍可能缓慢飘移，请用手护住手臂。确认进入？')) return
  armPost('hand_move')
}

function engageArm() {
  if (!confirm('获取控制会立即发布 rt/arm_sdk 接管手臂（真机！），并在当前姿态刚性保持。\n请确认没有其他程序（遥操作 / reach_server 等）在控制手臂。')) return
  armPost('engage')
}

function disarmArm() {
  const extra = arm.value?.float ? '当前处于卸力模式，' : ''
  if (!confirm(`${extra}归还控制后手臂交还本体控制器，权重 1 秒渐出——请扶住手臂。确认归还？`)) return
  armPost('disarm')
}

function jointShortName(name) {
  return name.replace(/^(left|right)_/, '').replace(/_joint$/, '')
}

// ---- 指尖尖点标定（pivot：多姿态触同一固定点，只用 FK，不用相机） ----

const pivotSamples = ref([])
const pivotResult = ref(null)
const pivotBusy = ref(false)

async function refreshPivot() {
  try {
    const data = await (await fetch('/api/pivot/samples')).json()
    pivotSamples.value = data.samples || []
  } catch { /* 后端未起 */ }
}

async function pivotAdd() {
  pivotBusy.value = true
  errorMsg.value = ''
  try {
    const res = await fetch('/api/pivot/samples', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    })
    const data = await res.json()
    if (!data.ok) errorMsg.value = data.error || '采样失败'
    else { pivotResult.value = null; await refreshPivot() }
  } catch (e) {
    errorMsg.value = String(e)
  } finally {
    pivotBusy.value = false
  }
}

async function pivotDelete(index) {
  await fetch(`/api/pivot/samples/${index}`, { method: 'DELETE' })
  pivotResult.value = null
  await refreshPivot()
}

async function pivotClear() {
  if (!confirm('清空所有尖点样本？')) return
  await fetch('/api/pivot/clear', { method: 'POST' })
  pivotResult.value = null
  await refreshPivot()
}

async function pivotSolve() {
  pivotBusy.value = true
  errorMsg.value = ''
  try {
    const res = await fetch('/api/pivot/solve', { method: 'POST' })
    const data = await res.json()
    if (data.ok) pivotResult.value = data
    else errorMsg.value = data.error || '解算失败'
  } finally {
    pivotBusy.value = false
  }
}

// 腕姿态摘要（rpy 度，URDF 固定轴约定），方便看 roll 是否转开了
function wristRpyDeg(T) {
  const R = T
  const pitch = Math.atan2(-R[2][0], Math.hypot(R[0][0], R[1][0]))
  let roll, yaw
  if (Math.abs(Math.cos(pitch)) < 1e-8) {
    roll = 0
    yaw = Math.atan2(-R[0][1], R[1][1])
  } else {
    roll = Math.atan2(R[2][1], R[2][2])
    yaw = Math.atan2(R[1][0], R[0][0])
  }
  return [roll, pitch, yaw].map((a) => (a * 180 / Math.PI).toFixed(0)).join('/')
}

// 卸力摆位时单手不方便点鼠标：按空格 = 「保持当前位置」
function onKeyDown(ev) {
  if (ev.code !== 'Space' || ev.repeat) return
  const tag = ev.target?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
  if (!arm.value?.float || armBusy.value) return
  ev.preventDefault()
  armPost('stop')
}

onMounted(async () => {
  window.addEventListener('keydown', onKeyDown)
  await refreshStatus()
  await refreshSamples()
  await refreshArm()
  await refreshPivot()
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
        机械臂停稳后，点击画面中的标记点（指尖/手背贴纸中心）。深度<b>只取你点中的那个像素</b>
        （8 帧时域中值，无空间外扩），该像素测不到深度会直接报错而不是拿背景顶替——
        指尖太黑/太细测不到时，贴一小块哑光贴纸最有效。
        自动位姿源会在点击的同一时刻抓取手腕位姿。
        <b>手腕的朝向也要在各样本间充分变化</b>，否则指尖偏移解不出来。
      </div>
    </div>

    <!-- 右：操作区 -->
    <div class="side-panel">
      <!-- 手臂点动（--arm-control 时显示） -->
      <div v-if="arm?.enabled" class="card">
        <h2>
          0. {{ arm.armed && arm.arm ? (arm.arm === 'right' ? '右' : '左') + '臂点动' : '手臂控制' }}
          <span class="badge" :class="!arm.armed ? '' : (arm.float ? 'bad' : (arm.jog_enabled ? 'good' : 'good'))">
            {{ !arm.armed ? '未接管' : (arm.float ? '卸力中（扶住！）' : (arm.jog_enabled ? '点动开启' : '刚性保持')) }}
          </span>
        </h2>

        <template v-if="!arm.armed">
          <div class="field-row">
            <label></label>
            <button class="btn primary" :disabled="armBusy" @click="engageArm">获取控制（真机接管）</button>
          </div>
          <div class="video-hint">
            未接管时本服务只读 rt/lowstate，不发布任何控制指令，可与其他控制程序并存。
            点「获取控制」后开始发布 rt/arm_sdk 并在当前姿态刚性保持——
            <b>确保没有其他程序在控制手臂</b>。
          </div>
        </template>

        <template v-else>
          <div class="field-row">
            <label>模式</label>
            <button v-if="!arm.jog_enabled && !arm.float" class="btn primary"
                    :disabled="armBusy" @click="armPost('enable_jog')">开启点动</button>
            <button v-if="arm.jog_enabled" class="btn"
                    :disabled="armBusy" @click="armPost('disable_jog')">停止点动</button>
            <button v-if="!arm.jog_enabled && !arm.float" class="btn warn"
                    :disabled="armBusy" @click="handMove">卸力拖动</button>
            <button v-if="arm.float" class="btn primary"
                    :disabled="armBusy" @click="armPost('stop')">保持当前位置（空格）</button>
            <button class="btn warn" :disabled="armBusy" @click="disarmArm">归还控制</button>
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
            卸力模式下 kp=0 只留阻尼 + 重力前馈（按实测角实时算），手臂近似失重、
            推到哪停哪；补偿有偏差时可能缓慢飘移，请护住手臂；
            摆好位置后点「保持当前位置」或<b>按空格</b>即锁定（单手扶臂时方便）。
            「归还控制」权重 1 秒渐出后交还本体控制器，请扶住手臂。
          </div>
        </template>
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

        <div class="field-row" style="margin-top: 12px;">
          <button class="btn" :disabled="samples.length < 3 || toolBusy" @click="solveToolOnly">
            {{ toolBusy ? '解算中…' : '只解指尖偏移（固定相机外参）' }}
          </button>
        </div>
        <div class="video-hint">
          已有可信的相机外参时用这个：每次点击<b>指尖的尖端</b>采样（不同手腕姿态、
          含反手大 roll），3 个样本起步、建议 ≥ 8。自动复用最新一份联合解算的
          T_base←camera，只解 3 个未知量，比联合解稳。
        </div>
        <template v-if="toolResult">
          <div style="margin-top: 8px; display: flex; gap: 8px; flex-wrap: wrap;">
            <span class="badge" :class="toolResult.residual_mm.rms < 5 ? 'good' : 'bad'">
              拟合 RMS {{ fmt(toolResult.residual_mm.rms, 2) }} mm
              （最大 {{ fmt(toolResult.residual_mm.max, 1) }}）
            </span>
            <span class="badge">
              p_tool(腕系) [{{ toolResult.p_tool_wrist_m.map((v) => fmt(v, 4)).join(', ') }}] m
            </span>
            <span class="badge">姿态跨度 {{ fmt(toolResult.wrist_rotation_spread_deg, 1) }}°</span>
            <span v-if="toolResult.delta_vs_calib_norm_mm != null" class="badge"
                  :class="toolResult.delta_vs_calib_norm_mm < 5 ? 'good' : 'bad'">
              与原 p_tool 差 {{ fmt(toolResult.delta_vs_calib_norm_mm, 1) }} mm
              [{{ toolResult.delta_vs_calib_mm.map((v) => fmt(v, 1)).join(', ') }}]
            </span>
            <span v-if="toolResult.dropped_samples?.length" class="badge bad">
              已自动剔除 {{ toolResult.dropped_samples.map((d) => `#${d.index}（${Math.round(d.residual_mm)}mm）`).join('、') }}
            </span>
            <span class="badge">实际参与 {{ toolResult.sample_indices.length }} 个样本</span>
          </div>
          <div class="video-hint">
            外参来自 {{ toolResult.calib_used }}；
            已生成替换 p_tool 的完整标定文件 {{ toolResult.merged_calib }}，
            可直接给 reach_server --calib 用（--tool-out-mm 记得给 0）。
          </div>
        </template>
      </div>

      <!-- 指尖尖点标定 -->
      <div class="card">
        <h2>4. 指尖尖点标定（多姿态触同一点，不用相机）</h2>
        <div class="video-hint">
          找一个固定的尖角参照物（桌角/螺丝尖）。用「卸力拖动」把<b>指尖顶在该点上</b>，
          点「保持当前位置」锁定后再点下面「采样当前姿态」；然后换一个手腕姿态
          （<b>务必包含反手大角度 roll</b>，就是拨开关那个姿态）重新顶到同一点，重复采样。
          建议 ≥ 6 个、姿态越分散越准。解算只用手腕 FK，不依赖相机；
          残差反映"各姿态下指尖没真正钉在同一点"的程度。
        </div>
        <div class="field-row" style="margin-top: 8px;">
          <label></label>
          <button class="btn primary" :disabled="pivotBusy" @click="pivotAdd">采样当前姿态</button>
          <button class="btn primary" :disabled="pivotSamples.length < 4 || pivotBusy" @click="pivotSolve">
            {{ pivotBusy ? '…' : `用 ${pivotSamples.length} 个姿态解算` }}
          </button>
          <button class="btn" :disabled="!pivotSamples.length" @click="pivotClear">清空</button>
        </div>
        <table v-if="pivotSamples.length">
          <thead>
            <tr><th>#</th><th>腕 t (m)</th><th>腕 rpy (°)</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="s in pivotSamples" :key="s.index">
              <td>{{ s.index }}</td>
              <td>{{ wristSummary(s.T_base_wrist) }}</td>
              <td>{{ wristRpyDeg(s.T_base_wrist) }}</td>
              <td><button class="del-btn" title="删除" @click="pivotDelete(s.index)">✕</button></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="coord dim">还没有尖点样本</div>
        <template v-if="pivotResult">
          <div style="margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;">
            <span class="badge" :class="pivotResult.residual_mm.rms < 5 ? 'good' : 'bad'">
              拟合 RMS {{ fmt(pivotResult.residual_mm.rms, 2) }} mm
            </span>
            <span v-if="pivotResult.leave_one_out_stats_mm" class="badge"
                  :class="pivotResult.leave_one_out_stats_mm.mean < 8 ? 'good' : 'bad'">
              留一验证均值 {{ fmt(pivotResult.leave_one_out_stats_mm.mean, 2) }} mm
            </span>
            <span class="badge">
              p_tool(腕系) [{{ pivotResult.p_tool_wrist_m.map((v) => fmt(v, 4)).join(', ') }}] m
            </span>
            <span class="badge">姿态跨度 {{ fmt(pivotResult.wrist_rotation_spread_deg, 1) }}°</span>
            <span v-if="pivotResult.delta_vs_handeye_norm_mm != null" class="badge"
                  :class="pivotResult.delta_vs_handeye_norm_mm < 5 ? 'good' : 'bad'">
              与手眼标定 p_tool 差 {{ fmt(pivotResult.delta_vs_handeye_norm_mm, 1) }} mm
              [{{ pivotResult.delta_vs_handeye_mm.map((v) => fmt(v, 1)).join(', ') }}]
            </span>
          </div>
          <div class="video-hint">
            已保存到 {{ pivotResult.saved_to }}
            <template v-if="pivotResult.merged_calib">
              ；同时生成了替换 p_tool 的完整标定文件 {{ pivotResult.merged_calib }}，
              可直接给 reach_server 的 --calib 使用（注意 --tool-out-mm 是否还需要）。
            </template>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

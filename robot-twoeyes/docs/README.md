# Robot TwoEyes — 双目视觉系统

基于 Jetson Orin NX 的双目立体视觉平台，集成标定、深度估计、目标检测、点云生成功能。

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     前端 (Vue 3 + Vite)                         │
│                     http://0.0.0.0:7009                         │
│  ┌─────────────────┐        ┌────────────────────────────────┐  │
│  │  深度采集页面     │        │  标定采集页面                    │  │
│  │  /depth          │        │  /calibrate                    │  │
│  └────────┬────────┘        └──────────────┬─────────────────┘  │
└───────────┼─────────────────────────────────┼───────────────────┘
            │ proxy                           │ proxy
            ▼                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│          深度 + 标定后端 (FastAPI)  端口 8124                     │
│                                                                 │
│  /api/*          → 深度采集、深度查询、配置                       │
│  /calibrate/*    → 标定采集（子应用挂载）                         │
│  /ws/stream      → 左眼实时预览 WebSocket                        │
└───────────────┬─────────────────────────────────────────────────┘
                │ HTTP (切换到 CREStereo 时)
                ▼
┌───────────────────────────┐    ┌────────────────────────────────┐
│  CREStereo 服务  端口 8126 │    │  YOLO 检测服务  端口 8125       │
│  /api/disparity           │    │  /api/detect, /api/segment     │
│  /api/status              │    │  /api/status, /api/classes     │
└───────────────────────────┘    └────────────────────────────────┘
```

---

## 模块说明

### 模块 1：深度 + 标定后端 (`depth/` + `backend/`)

| 项目 | 内容 |
|---|---|
| 端口 | 8124 |
| 环境 | conda: `fastapi` (Python 3.9) |
| 入口 | `depth/run_server.py` |
| 功能 | 双目深度估计、点云生成、立体标定图像采集 |

**输入：** 通过 teleimager SDK 从双目相机实时获取左右图像对 (640x480 BGR)。

**输出：**
- 矫正后的左眼图像 (JPEG)
- 深度伪彩色图 (JPEG)
- 深度数据 (.npy, float32, 单位 mm)
- 点云文件 (.ply, 含 RGB + 径向距离)
- 标定图像对 (left/right JPEG)

**核心处理流程：**
1. 从相机获取原始左右图
2. 使用标定参数进行立体矫正 (rectify)
3. 计算视差图（SGBM 或 CREStereo 可选）
4. 视差 → Z 深度 → 径向距离
5. 导出 PLY 点云

**启动参数：**
```bash
python depth/run_server.py \
  --calib_path <标定yaml路径> \
  --save_path ./data/depth_captures \
  --port 8124 \
  --disparity-method sgbm \
  --no-wls            # 可选：默认关闭 WLS
```

---

### 模块 2：YOLO 目标检测服务 (`img_process/yolo/`)

| 项目 | 内容 |
|---|---|
| 端口 | 8125 |
| 环境 | conda: `yolo` (Python 3.8) |
| 入口 | `img_process/yolo/run_server.py` |
| 功能 | YOLO11 ONNX 目标检测 + 实例分割 |

**输入：** 单张图像（文件上传或 base64 编码）。

**输出：**
- 检测结果列表：边界框 (bbox)、置信度、类别
- 分割掩码轮廓点集 (contour)
- 可选：标注后的图像

**模型：** `img_process/yolo/model/XiongMao1.onnx` (YOLOv11 segmentation, 1 类: XiongMao)

**启动参数：**
```bash
python img_process/yolo/run_server.py \
  --model img_process/yolo/model/XiongMao1.onnx \
  --class-names XiongMao \
  --port 8125 \
  --conf 0.25 \
  --iou 0.45
```

---

### 模块 3：CREStereo 视差估计服务 (`img_process/crestereo/`)

| 项目 | 内容 |
|---|---|
| 端口 | 8126 |
| 环境 | conda: `yolo` (Python 3.8) |
| 入口 | `img_process/crestereo/run_server.py` |
| 功能 | 基于深度学习的立体匹配，替代传统 SGBM |

**输入：** 矫正后的左右图像对 (base64 JPEG)。

**输出：** 视差图 (float32 数组, base64 编码)。

**模型选项 (PINTO_model_zoo #284)：**

| 模型 | 输入尺寸 | 模式 | Orin NX 推理时间 |
|---|---|---|---|
| `crestereo_init_iter2_480x640.onnx` | 480x640 | 单程 | ~数秒 |
| `crestereo_init_iter5_480x640.onnx` | 480x640 | 单程 | ~十余秒 |
| `crestereo_combined_iter5_480x640.onnx` | 480x640 | 两程 | ~数十秒 |
| `crestereo_combined_iter10_480x640.onnx` | 480x640 | 两程 | ~71 秒 |

**启动参数：**
```bash
python img_process/crestereo/run_server.py \
  --model img_process/crestereo/model/crestereo_combined_iter10_480x640.onnx \
  --port 8126 \
  --no-gpu  # 可选：禁用 GPU
```

---

### 模块 4：前端 (`frontend/`)

| 项目 | 内容 |
|---|---|
| 端口 | 7009 |
| 框架 | Vue 3 + Vite + Vue Router |
| 入口 | `frontend/src/main.js` |

**页面：**

| 路由 | 页面 | 功能 |
|---|---|---|
| `/depth` | 深度采集 | 实时预览、深度采集、YOLO 检测、点击查深度 |
| `/calibrate` | 标定采集 | 棋盘格角点检测、左右图采集 |

**前端配置代理 (vite.config.js)：**
- `/api/*` → `localhost:8124`
- `/calibrate/api/*` → `localhost:8124`
- `/ws/*` → `ws://localhost:8124`
- YOLO 请求直接跨域到 `localhost:8125`

---

## API 详细说明

### 一、深度后端 API (端口 8124)

#### `POST /api/capture`

采集一组立体图像，计算深度并生成点云。

**请求：** 无参数（从相机实时获取）。

**响应：**
```json
{
  "success": true,
  "index": 0,
  "count": 1,
  "num_points": 245000,
  "depth_viz": "<base64 JPEG 深度伪彩色图>",
  "left_image": "<base64 JPEG 矫正后左图>"
}
```

**保存的文件（data/depth_captures/{时间戳}/）：**
- `left_0000.jpg` — 矫正后左图
- `depth_0000.npy` — 深度数据 (float32, mm)
- `depth_viz_0000.jpg` — 深度伪彩色图
- `pointcloud_0000.ply` — 点云文件

---

#### `GET /api/depth_at?index={n}&x={px}&y={py}`

查询指定像素的深度值。

**参数：**
- `index` — 采集编号
- `x`, `y` — 像素坐标（矫正后左图）

**响应：**
```json
{
  "x": 320,
  "y": 240,
  "depth_mm": 456.7,
  "index": 0
}
```

---

#### `GET /api/history`

列出所有已采集的深度图。

**响应：**
```json
{
  "captures": ["0000", "0001", "0002"],
  "count": 3
}
```

---

#### `GET /api/images/{filename}`

获取已保存的图像文件。

**示例：** `GET /api/images/depth_viz_0000.jpg`

---

#### `POST /api/config`

动态修改运行时配置。

**请求体：**
```json
{
  "use_wls": true,
  "disparity_method": "sgbm"
}
```

- `use_wls` (bool) — 是否启用 WLS 滤波（仅对 SGBM 有效）
- `disparity_method` ("sgbm" | "crestereo") — 视差算法

**响应：**
```json
{
  "use_wls": true,
  "disparity_method": "sgbm"
}
```

---

#### `GET /api/status`

获取当前后端状态。

**响应：**
```json
{
  "count": 5,
  "save_path": "data/depth_captures/202605271212",
  "image_size": "640x480",
  "use_wls": true,
  "has_ximgproc": true,
  "disparity_method": "sgbm"
}
```

---

#### `WebSocket /ws/stream`

实时推送左眼画面。

**推送格式 (JSON)：**
```json
{
  "left": "<base64 JPEG>"
}
```

---

### 二、标定子应用 API (端口 8124, 路径前缀 /calibrate)

#### `POST /calibrate/api/capture`

采集一组标定图像。

**响应：**
```json
{
  "success": true,
  "index": 0,
  "count": 1
}
```

**保存的文件（data/calib_images/{时间戳}/）：**
- `left/0000.jpg`
- `right/0000.jpg`

---

#### `GET /calibrate/api/history`

列出所有已采集的标定图像。

**响应：**
```json
{
  "images": ["0000.jpg", "0001.jpg"],
  "count": 2
}
```

---

#### `GET /calibrate/api/images/{side}/{filename}?corners=1&board_size=11x8`

获取标定图像，可选叠加角点绘制。

**参数：**
- `side` — "left" 或 "right"
- `filename` — 文件名
- `corners` (可选) — 设为 1 则绘制角点
- `board_size` (可选) — 棋盘格尺寸，如 "11x8"

---

#### `POST /calibrate/api/config`

修改棋盘格尺寸。

**请求体：**
```json
{
  "board_size": "11x8"
}
```

---

#### `GET /calibrate/api/status`

**响应：**
```json
{
  "count": 10,
  "board_size": "11x8",
  "save_path": "data/calib_images/202605271219"
}
```

---

#### `WebSocket /calibrate/ws/stream`

实时推送左右眼画面（含角点检测结果）。

**推送格式 (JSON)：**
```json
{
  "left": "<base64 JPEG>",
  "right": "<base64 JPEG>",
  "left_detected": true,
  "right_detected": true,
  "count": 10
}
```

**接收命令 (JSON)：**
```json
{"show_corners": true}
{"board_size": "11x8"}
```

---

### 三、YOLO 检测 API (端口 8125)

#### `POST /api/detect`

上传图像，返回目标检测结果（仅边界框）。

**请求：** `multipart/form-data`, 字段名 `file`。

```bash
curl -X POST http://localhost:8125/api/detect -F "file=@image.jpg"
```

**响应：**
```json
{
  "detections": [
    {
      "bbox": [322.3, 259.6, 420.6, 301.4],
      "confidence": 0.91,
      "class_id": 0,
      "class_name": "XiongMao"
    }
  ],
  "count": 1,
  "image_size": [640, 480]
}
```

`bbox` 格式为 `[x1, y1, x2, y2]`（左上角、右下角像素坐标）。

---

#### `POST /api/segment`

上传图像，返回检测结果 + 分割掩码轮廓。

**请求：** 同 `/api/detect`。

**响应：**
```json
{
  "detections": [
    {
      "bbox": [322.3, 259.6, 420.6, 301.4],
      "confidence": 0.91,
      "class_id": 0,
      "class_name": "XiongMao",
      "contour": [[330, 261], [331, 262], ...]
    }
  ],
  "masks": ["<base64 PNG 二值掩码>"],
  "count": 1,
  "image_size": [640, 480]
}
```

`contour` 为分割掩码的轮廓点集 `[[x,y], ...]`。

---

#### `POST /api/detect_base64`

接受 base64 编码的图像。

**请求体：**
```json
{
  "image": "<base64 JPEG>"
}
```

---

#### `POST /api/detect_draw`

上传图像，返回检测结果 + 标注后的图像。

**请求：** 同 `/api/detect`。

**响应额外字段：**
```json
{
  "annotated_image": "<base64 JPEG 标注图>"
}
```

---

#### `POST /api/detect_bytes`

接受原始 BGR 字节数据（用于模块间内部调用）。

**请求体：**
```json
{
  "data": "<base64 of raw BGR bytes>",
  "height": 480,
  "width": 640
}
```

---

#### `GET /api/status`

**响应：**
```json
{
  "model_loaded": true,
  "model_path": "img_process/yolo/model/XiongMao1.onnx",
  "model_type": "segment",
  "input_size": "640x640",
  "num_classes": 1,
  "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
  "conf_threshold": 0.25,
  "iou_threshold": 0.45
}
```

---

#### `GET /api/classes`

**响应：**
```json
{
  "classes": ["XiongMao"]
}
```

---

### 四、CREStereo API (端口 8126)

#### `POST /api/disparity`

从矫正后的立体图像对计算视差图。

**请求体：**
```json
{
  "left": "<base64 JPEG 矫正后左图>",
  "right": "<base64 JPEG 矫正后右图>"
}
```

**响应：**
```json
{
  "disparity": "<base64 float32 原始字节>",
  "height": 480,
  "width": 640,
  "inference_time": 71.189
}
```

**解码视差图 (Python)：**
```python
import base64, numpy as np
disp_bytes = base64.b64decode(data["disparity"])
disp = np.frombuffer(disp_bytes, dtype=np.float32).reshape(data["height"], data["width"])
```

---

#### `GET /api/status`

**响应：**
```json
{
  "model_loaded": true,
  "model_path": "img_process/crestereo/model/crestereo_combined_iter10_480x640.onnx",
  "input_size": "640x480",
  "is_combined": true,
  "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"]
}
```

---

## systemctl 服务管理

### 服务列表

| 服务名 | 说明 | 环境 |
|---|---|---|
| `robot-twoeyes.service` | 深度 + 标定后端 | conda: fastapi |
| `yolo-detect.service` | YOLO 目标检测 | conda: yolo |
| `crestereo.service` | CREStereo 视差估计 | conda: yolo |
| `robot-twoeyes-web.service` | 前端 Vue dev server | Node.js (NVM) |

### 安装 & 开机自启

```bash
sudo bash services/install.sh
```

该脚本会复制所有 `.service` 文件到 `/etc/systemd/system/`，执行 `daemon-reload`，并 `enable` 四个服务（开机自启）。

### 常用命令

```bash
# 启动全部
sudo systemctl start yolo-detect robot-twoeyes robot-twoeyes-web crestereo

# 停止全部
sudo systemctl stop yolo-detect robot-twoeyes robot-twoeyes-web crestereo

# 查看状态
systemctl status yolo-detect robot-twoeyes robot-twoeyes-web crestereo

# 查看日志（实时跟踪）
journalctl -u robot-twoeyes -f
journalctl -u yolo-detect -f
journalctl -u crestereo -f
journalctl -u robot-twoeyes-web -f

# 重启单个服务
sudo systemctl restart robot-twoeyes
```

---

## 目录结构

```
robot-twoeyes/
├── backend/                    # 标定采集模块
│   ├── main.py                 #   FastAPI 子应用
│   ├── camera.py               #   Teleimager 相机封装
│   └── detection.py            #   棋盘格角点检测
├── depth/                      # 深度采集模块
│   ├── main.py                 #   FastAPI 主应用
│   ├── run_server.py           #   启动入口
│   ├── stereo_depth.py         #   立体矫正 + 视差 + 深度
│   └── ply_export.py           #   PLY 点云导出
├── img_process/
│   ├── yolo/                   # YOLO 检测模块
│   │   ├── main.py             #   FastAPI 应用
│   │   ├── run_server.py       #   启动入口
│   │   ├── detector.py         #   ONNX 推理封装
│   │   └── model/              #   ONNX 模型文件
│   └── crestereo/              # CREStereo 视差模块
│       ├── main.py             #   FastAPI 应用
│       ├── run_server.py       #   启动入口
│       ├── estimator.py        #   ONNX 推理封装
│       └── model/              #   ONNX 模型文件
├── frontend/                   # Vue 前端
│   ├── src/
│   │   ├── main.js             #   入口 + 路由配置
│   │   ├── RootApp.vue         #   根组件（导航栏）
│   │   └── views/
│   │       ├── DepthCapture.vue      # 深度采集页面
│   │       └── CalibrationCapture.vue # 标定采集页面
│   ├── vite.config.js          #   Vite 配置（端口 + 代理）
│   └── package.json
├── services/                   # systemctl 服务文件
│   ├── robot-twoeyes.service
│   ├── yolo-detect.service
│   ├── crestereo.service
│   ├── robot-twoeyes-web.service
│   └── install.sh
├── data/
│   ├── depth_captures/         # 深度采集数据输出
│   └── calib_images/           # 标定图像输出
└── docs/
    └── README.md               # 本文档
```

---

## 硬件环境

| 项目 | 配置 |
|---|---|
| 计算平台 | NVIDIA Jetson Orin NX Developer Kit |
| JetPack | R35.3.1 (L4T) |
| CUDA | 11.4 |
| cuDNN | 8.6.0 |
| 相机 | 双目广角相机 (FOV ~100°, 640x480) |
| 基线 | ~60 mm |

## Python 环境

| conda 环境 | Python | 用途 | 关键依赖 |
|---|---|---|---|
| `fastapi` | 3.9 | 深度 + 标定后端 | fastapi, uvicorn, opencv, numpy, teleimager |
| `yolo` | 3.8 | YOLO + CREStereo | onnxruntime-gpu 1.17, fastapi, opencv, numpy |

---

## 相机内参

（来自 `data/calib_images/202605261533/stereo_calibration.yaml`）

**左相机：**
- 焦距: fx=269.0, fy=270.2 (px)
- 光心: cx=305.9, cy=269.8 (px)
- 水平 FOV: ~100°, 垂直 FOV: ~83°

**基线：** ~60.2 mm

**坐标系：** 原点为左相机光心，X 向右，Y 向下，Z 向前。深度值为从光心到物体的距离，与镜头表面有约 3-4cm 偏差。

# 双目标定 Web 应用设计方案

## 需求总结

将双目标定图像采集从桌面 GUI (OpenCV/Tk) 迁移为 Web 应用，方便在浏览器中操作。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI + WebSocket |
| 前端 | Vue 3 + Vite (开发模式) |
| 相机接口 | `capture_head_images` (Unitree G1 头部双目) |

## 功能列表

### 1. 实时预览 (WebSocket, 目标 25Hz)

- 后端持续抓取左右相机画面
- 每帧做棋盘格角点检测
- 通过 WebSocket 推送 JPEG 编码的帧到前端
- 前端左右并排显示
- 支持"显示/隐藏角点"开关（前端切换，后端根据状态决定是否绘制角点）

### 2. 拍照保存

- 前端点击"拍照"按钮
- 后端保存当前帧到磁盘
- 保存路径通过后端启动时命令行参数 `--save_path` 指定
- 目录结构：
  ```
  save_path/
  ├── left/
  │   ├── 0000.jpg
  │   ├── 0001.jpg
  │   └── ...
  └── right/
      ├── 0000.jpg
      ├── 0001.jpg
      └── ...
  ```

### 3. 历史图像浏览

- 前端显示已拍摄图像的缩略图列表
- 点击某组图像弹出大弹窗预览
- 弹窗中左右图像并排显示
- 弹窗支持"显示/隐藏角点"切换

### 4. 状态显示

- 左相机角点检测状态 (成功/失败)
- 右相机角点检测状态 (成功/失败)
- 已拍摄组数
- 棋盘格参数 (可在前端配置)

## 架构图

```
┌─────────────────────────────────────────────────────┐
│  浏览器 (Vue 3)                                      │
│                                                     │
│  ┌───────────┐  ┌───────────┐  ┌────────────────┐  │
│  │ 左相机预览 │  │ 右相机预览 │  │ 历史图像列表   │  │
│  └───────────┘  └───────────┘  └────────────────┘  │
│       ▲               ▲              ▲              │
│       └───────┬───────┘              │              │
│           WebSocket                REST API         │
└───────────────┼──────────────────────┼──────────────┘
                │                      │
┌───────────────┼──────────────────────┼──────────────┐
│  FastAPI 后端 │                      │              │
│               ▼                      ▼              │
│  ┌─────────────────┐    ┌─────────────────────┐    │
│  │ 视频流 WebSocket │    │ REST: 拍照/历史列表  │    │
│  │ - 抓帧           │    │ POST /capture        │    │
│  │ - 角点检测       │    │ GET  /history        │    │
│  │ - JPEG编码推送   │    │ GET  /images/{name}  │    │
│  └─────────────────┘    └─────────────────────┘    │
│           │                                         │
│           ▼                                         │
│  ┌─────────────────┐                               │
│  │ 相机接口         │                               │
│  │ capture_head_images(host, wait_sec)             │
│  └─────────────────┘                               │
└─────────────────────────────────────────────────────┘
```

## API 设计

### WebSocket `/ws/stream`

- 后端推送 JSON 消息：
  ```json
  {
    "left": "<base64 JPEG>",
    "right": "<base64 JPEG>",
    "left_detected": true,
    "right_detected": false,
    "count": 5
  }
  ```
- 前端可发送控制消息：
  ```json
  {"show_corners": true}
  {"board_size": "9x6"}
  ```

### REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/capture` | 拍照保存当前帧 |
| GET | `/api/history` | 获取已拍摄图像列表 |
| GET | `/api/images/{side}/{filename}` | 获取某张图像 |
| GET | `/api/status` | 获取当前状态 (拍摄数量等) |
| POST | `/api/config` | 更新棋盘格参数 |

## 后端启动方式

```bash
cd /path/to/project
# 需要在能 import capture_head_images 的环境中运行
PYTHONPATH="$PWD:$PWD/scripts" python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

或封装为：

```bash
python run_server.py --save_path ./calib_images --host 127.0.0.1 --board_size 9x6 --port 8000
```

## 前端启动方式

```bash
cd frontend
npm install
npm run dev
# 访问 http://localhost:5173
```

## 关于 25Hz 帧率

能否达到 25Hz 取决于 `capture_head_images` 的调用耗时：

- 如果该函数内部是从持续的视频流/ROS topic 获取最新帧 → 可以做到 25Hz
- 如果每次调用都要等待曝光 (wait_sec=2~5s) → 无法做到，实际帧率约 0.2~0.5Hz

**建议**：后端设计为可配置帧率，默认尽可能快地循环抓帧推送。如果相机接口慢，前端体验为"每隔几秒刷新一帧"也是可以接受的。

## 待确认

1. `capture_head_images` 的实际调用耗时（决定帧率上限）
2. 是否需要在前端配置相机 IP (host)
3. 是否需要删除已拍摄图像的功能

## 项目目录结构（预期）

```
two-eyes/
├── backend/
│   ├── main.py              # FastAPI 应用入口
│   ├── camera.py            # 相机接口封装
│   ├── detection.py         # 角点检测逻辑
│   └── requirements.txt     # Python 依赖
├── frontend/
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── App.vue
│       ├── main.js
│       └── components/
│           ├── StreamView.vue      # 实时预览
│           ├── CaptureButton.vue   # 拍照按钮
│           ├── HistoryPanel.vue    # 历史图像列表
│           └── PreviewModal.vue    # 大图预览弹窗
├── capture_stereo.py        # 命令行版采集脚本（已有）
├── run_server.py            # 服务启动入口
└── stereo_calibration_web_plan.md  # 本文档
```

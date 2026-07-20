# Hand-Eye 3D — 眼在手外标定（联合估计指尖偏移，免棋盘格）

利用深度相机能直接给三维坐标的特点做手眼标定：同一个物理标记点
（灵巧手指尖 / 手背贴纸），相机侧点击画面得 \(P_{camera}\)，机器人侧
只需提供**手腕位姿** \(T_{base}^{wrist}\)（DDS 自动读取）。求解器把
相机外参和指尖偏移一起解出来，**不需要事先测量指尖装在哪**：

\[ R\,P_{camera,i} + t \;=\; R_{w,i}\,p_{tool} + t_{w,i} \]

未知量：\(T_{base}^{camera}\)（6 维）+ \(p_{tool}\)（腕系下指尖偏移，3 维）。
交替最小二乘（固定 \(p_{tool}\) 是 Kabsch 闭式解，固定 \(T\) 是线性 LS），
单调收敛。已用仿真验证：14 样本 + 2mm 噪声下旋转误差 0.1°、平移 1.9mm、
\(p_{tool}\) 误差 1.7mm。

与 `../hand_eye`（棋盘格 + `cv2.calibrateHandEye`）互补：本方法不用打印
标定板、上手快，精度约 3–8mm；追求更高精度用那套。

## 坐标系约定

- \(P_{camera}\)：**彩色相机坐标系**（深度经 AlignFilter 对齐到彩色后用彩色
  内参反投影；X 右、Y 下、Z 前，米）。与 video_tools 的彩色点云同系，
  解出的 T 可直接用于点云。
- \(T_{base}^{wrist}\)：H2 模式下 base = `torso_link`、wrist = `right_wrist_yaw_link`
  （取自 IK_replay 的 h2.yaml），FK 只用手臂 7 关节。

## 目录

```
backend/
  solver.py    Kabsch + 联合解(交替 LS) + 留一验证 + 退化检测
  camera.py    Orbbec RGBD（pyorbbecsdk）+ mock
  robot.py     手腕位姿 Provider：manual / http / h2(DDS+FK) / mock
  app.py       FastAPI：预览、点击反投影、样本管理、解算
run_server.py  入口（后端 8132）
frontend/      Vue3 + Vite 界面（端口 7012）
```

## 环境

```bash
pip install -r backend/requirements.txt   # fastapi uvicorn numpy opencv-python pyorbbecsdk2
cd frontend && npm install
```

H2 模式额外需要 `unitree_sdk2py` + `cyclonedds`（目前这台机器只有
`unifolm-wma` 环境装了）。三种方案任选：装进你的环境 / 直接用
unifolm-wma 环境跑本服务 / 在 unifolm-wma 里跑一个 pose sidecar 走
`--pose-source http`。

## 启动

```bash
# H2 真机（推荐）：DDS 只读 rt/lowstate + IK_replay FK，右臂
python run_server.py --camera-source orbbec --camera-serial CP0BB53000FS \
    --pose-source h2 --network-interface eth0

# 手腕位姿手填（任何机器人可用）
python run_server.py --camera-source orbbec

# 纯联调
python run_server.py --camera-source mock --pose-source mock

# 前端
cd frontend && npm run dev     # http://<IP>:7012
```

> H2 模式**只订阅** `rt/lowstate`，绝不发布 `rt/arm_sdk`/`rt/lowcmd`，
> 与现有控制程序并存不会引起抢占/抽搐。摆位姿用你现有的控制方式。

## 操作流程

1. 灵巧手保持**固定手势**（整个标定期间不许变，p_tool 是常量的前提），
   在手背/指节贴一块哑光标记。
2. 机械臂移到新位姿并停稳（位置撒满任务空间，**手腕朝向也要充分变化**，
   姿态跨度 < 15° 时求解器会拒绝解算——朝向不变的话 p_tool 和 t 分不开）。
3. 网页点击标记点 → 得 \(P_{camera}\)（8 帧 × 5×5 窗口中值，自动拒绝飞点）；
   h2/http 模式会在同一时刻自动抓取手腕位姿，manual 模式手填 xyz+rpy。
4. 「保存这个样本」。重复 12–20 次。
5. 「解算」→ 输出 4×4 矩阵、RPY、p_tool、拟合 RMS、留一交叉验证；
   存到 `<save_path>/handeye3d_result.json`。

验收参考：拟合 RMS < 8mm、留一均值 < 10mm。超标常见原因：采样时手臂没
停稳、点到深度飞点、灵巧手手势中途变了。删掉可疑样本重解即可。

## 结果怎么用

```python
import json, numpy as np
r = json.load(open("handeye3d_result.json"))
T = np.array(r["T_cam2base"])              # torso_link <- 彩色相机
p_base = (T @ np.append(p_camera, 1.0))[:3]
p_tool = np.array(r["p_tool_wrist_m"])     # 顺带解出的指尖在腕系的位置
```

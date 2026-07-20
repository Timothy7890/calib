"""3D-3D 手眼求解。

两种模式:
1. solve_rigid_transform: 已知点对 (P_camera_i, P_base_i)，Kabsch 闭式解 T_base^camera。
2. solve_with_tool_offset: 标记点在基座系的坐标未知，只知道每次采样时的
   手腕位姿 T_base^wrist_i；把指尖偏移 p_tool（腕系下，常量）和 T_base^camera
   联合解出。约束: R @ P_cam_i + t = R_w_i @ p_tool + t_w_i。
   用交替最小二乘：固定 p_tool 是 Kabsch，固定 (R,t) 是线性最小二乘，
   两步都各自全局最优，从 p_tool=0 出发单调下降收敛。

单位一律米。
"""

from __future__ import annotations

import math

import numpy as np

MIN_SAMPLES = 3
MIN_SAMPLES_TOOL = 5  # 联合解 9 个未知量，5 对(15 方程)起步，建议 >= 10
MIN_WRIST_ROT_DEG = 15.0  # 手腕姿态变化不足时 p_tool 与 t 不可分


def rpy_to_rot(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """URDF 固定轴 RPY → 旋转矩阵（R = Rz(yaw) Ry(pitch) Rx(roll)）。"""
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    Rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    Ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    Rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def make_T(R: np.ndarray, t) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = np.asarray(t, dtype=float).reshape(3)
    return T


def geodesic_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    c = (np.trace(Ra.T @ Rb) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def rot_to_rpy(R: np.ndarray) -> tuple[float, float, float]:
    """URDF 固定轴 RPY 约定（R = Rz(yaw) Ry(pitch) Rx(roll)），与 hand_eye 项目一致。"""
    pitch = math.atan2(-R[2, 0], math.hypot(R[0, 0], R[1, 0]))
    if abs(math.cos(pitch)) < 1e-8:
        roll = 0.0
        yaw = math.atan2(-R[0, 1], R[1, 1])
    else:
        roll = math.atan2(R[2, 1], R[2, 2])
        yaw = math.atan2(R[1, 0], R[0, 0])
    return roll, pitch, yaw


def _collinearity(points: np.ndarray) -> float:
    """点集第二奇异值与第一奇异值之比，接近 0 说明近似共线（解不稳定）。"""
    centered = points - points.mean(axis=0)
    s = np.linalg.svd(centered, compute_uv=False)
    if s[0] < 1e-12:
        return 0.0
    return float(s[1] / s[0])


def solve_rigid_transform(p_camera: np.ndarray, p_base: np.ndarray) -> dict:
    """Kabsch 算法求 R, t 使 ||R @ p_camera + t - p_base|| 最小。

    p_camera, p_base: (N, 3)，米。返回包含 T、残差统计的 dict。
    """
    p_camera = np.asarray(p_camera, dtype=float).reshape(-1, 3)
    p_base = np.asarray(p_base, dtype=float).reshape(-1, 3)
    n = len(p_camera)
    if len(p_base) != n:
        raise ValueError(f"点数不一致: camera {n} vs base {len(p_base)}")
    if n < MIN_SAMPLES:
        raise ValueError(f"至少需要 {MIN_SAMPLES} 对点，当前 {n} 对")

    collinearity = _collinearity(p_camera)
    if collinearity < 1e-6:
        raise ValueError("采样点几乎共线，无法唯一确定旋转，请把点在空间中撒开")

    centroid_cam = p_camera.mean(axis=0)
    centroid_base = p_base.mean(axis=0)
    q_cam = p_camera - centroid_cam
    q_base = p_base - centroid_base

    H = q_cam.T @ q_base
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    D = np.diag([1.0, 1.0, d])  # 修正镜像解
    R = Vt.T @ D @ U.T
    t = centroid_base - R @ centroid_cam

    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t

    # 逐点残差（毫米）
    predicted = (R @ p_camera.T).T + t
    errors_mm = np.linalg.norm(predicted - p_base, axis=1) * 1000.0
    rpy = rot_to_rpy(R)

    return {
        "num_samples": n,
        "T_cam2base": T.tolist(),
        "R_cam2base": R.tolist(),
        "t_cam2base_m": t.tolist(),
        "rpy_rad": list(rpy),
        "rpy_deg": [math.degrees(a) for a in rpy],
        "residual_mm": {
            "per_sample": errors_mm.tolist(),
            "rms": float(np.sqrt((errors_mm ** 2).mean())),
            "mean": float(errors_mm.mean()),
            "max": float(errors_mm.max()),
        },
        "collinearity": collinearity,
    }


def solve_with_tool_offset(p_camera: np.ndarray, T_wrist: np.ndarray,
                           max_iters: int = 200, tol: float = 1e-10) -> dict:
    """联合估计 T_base^camera 和指尖偏移 p_tool（腕系）。

    p_camera: (N,3) 相机系坐标（米）
    T_wrist:  (N,4,4) 每次采样时的手腕位姿 T_base^wrist
    """
    p_camera = np.asarray(p_camera, dtype=float).reshape(-1, 3)
    T_wrist = np.asarray(T_wrist, dtype=float).reshape(-1, 4, 4)
    n = len(p_camera)
    if len(T_wrist) != n:
        raise ValueError(f"点数不一致: camera {n} vs wrist {len(T_wrist)}")
    if n < MIN_SAMPLES_TOOL:
        raise ValueError(f"联合解至少需要 {MIN_SAMPLES_TOOL} 对样本，当前 {n} 对")

    R_w = T_wrist[:, :3, :3]
    t_w = T_wrist[:, :3, 3]

    # 可辨识性检查：手腕姿态必须有足够变化，否则 p_tool 与 t 耦合不可分
    max_rot = max(geodesic_deg(R_w[0], R_w[i]) for i in range(1, n))
    if max_rot < MIN_WRIST_ROT_DEG:
        raise ValueError(
            f"手腕姿态变化只有 {max_rot:.1f}°（需要 ≥ {MIN_WRIST_ROT_DEG}°），"
            "请让手腕朝向也充分变化，否则指尖偏移无法解出")

    p_tool = np.zeros(3)
    prev_cost = np.inf
    iterations = 0
    for iterations in range(1, max_iters + 1):
        # 步骤 1: 固定 p_tool，P_base_i = R_w_i @ p_tool + t_w_i，Kabsch 解 (R, t)
        p_base = (R_w @ p_tool) + t_w
        base = solve_rigid_transform(p_camera, p_base)
        R = np.array(base["R_cam2base"])
        t = np.array(base["t_cam2base_m"])

        # 步骤 2: 固定 (R, t)，线性最小二乘解 p_tool:
        #   R_w_i @ p_tool = (R @ p_cam_i + t) - t_w_i
        target = (R @ p_camera.T).T + t - t_w          # (N,3)
        A = R_w.reshape(-1, 3)                          # (3N,3)
        b = target.reshape(-1)
        p_tool_new, *_ = np.linalg.lstsq(A, b, rcond=None)

        residual = (R_w @ p_tool_new) + t_w - target
        cost = float((residual ** 2).sum())
        if abs(prev_cost - cost) < tol:
            p_tool = p_tool_new
            break
        p_tool = p_tool_new
        prev_cost = cost

    # 最终一轮解算 + 残差
    p_base = (R_w @ p_tool) + t_w
    result = solve_rigid_transform(p_camera, p_base)
    result["mode"] = "tool_offset_joint"
    result["p_tool_wrist_m"] = p_tool.tolist()
    result["iterations"] = iterations
    result["wrist_rotation_spread_deg"] = max_rot
    return result


def leave_one_out_tool(p_camera: np.ndarray, T_wrist: np.ndarray) -> list[float]:
    """联合解的留一交叉验证（毫米）。"""
    p_camera = np.asarray(p_camera, dtype=float).reshape(-1, 3)
    T_wrist = np.asarray(T_wrist, dtype=float).reshape(-1, 4, 4)
    n = len(p_camera)
    if n < MIN_SAMPLES_TOOL + 1:
        return []
    errors = []
    for i in range(n):
        mask = np.arange(n) != i
        try:
            res = solve_with_tool_offset(p_camera[mask], T_wrist[mask])
        except ValueError:
            errors.append(float("nan"))
            continue
        R = np.array(res["R_cam2base"])
        t = np.array(res["t_cam2base_m"])
        p_tool = np.array(res["p_tool_wrist_m"])
        pred_base = R @ p_camera[i] + t
        true_base = T_wrist[i, :3, :3] @ p_tool + T_wrist[i, :3, 3]
        errors.append(float(np.linalg.norm(pred_base - true_base) * 1000.0))
    return errors


def leave_one_out(p_camera: np.ndarray, p_base: np.ndarray) -> list[float]:
    """留一交叉验证：每次剔除一个点解算，再用该点评估预测误差（毫米）。

    比拟合残差更诚实地反映真实精度。点数 < MIN_SAMPLES+1 时返回空列表。
    """
    p_camera = np.asarray(p_camera, dtype=float).reshape(-1, 3)
    p_base = np.asarray(p_base, dtype=float).reshape(-1, 3)
    n = len(p_camera)
    if n < MIN_SAMPLES + 1:
        return []
    errors = []
    for i in range(n):
        mask = np.arange(n) != i
        try:
            res = solve_rigid_transform(p_camera[mask], p_base[mask])
        except ValueError:
            errors.append(float("nan"))
            continue
        R = np.array(res["R_cam2base"])
        t = np.array(res["t_cam2base_m"])
        pred = R @ p_camera[i] + t
        errors.append(float(np.linalg.norm(pred - p_base[i]) * 1000.0))
    return errors

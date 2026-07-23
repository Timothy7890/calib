"""
5D suction-cup inverse kinematics solver.

This module implements strict 3D position + 2D axis-orientation IK for a
7-DOF manipulator, but the math is written for a generic N-DOF robot.

Typical suction grasp state-machine sequence, implemented outside this file:
1. Stereo vision outputs target_position_base.
2. Use a configured fixed target_axis_base, for example [0, 0, -1].
3. compute_pre_approach_position() gives p_pre.
4. call solve(q_current, p_pre, target_axis_base) to reach pre-approach.
5. Move slowly along target_axis_base toward the target.
6. External controller turns on the vacuum pump after contact.
7. Lift after suction succeeds.

No target normal estimation, temporal filtering, pump control, ROS node, or
full 6D orientation constraint is implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np


def normalize(v, eps: float = 1e-9) -> np.ndarray:
    """返回单位向量；范数过小时抛出 ValueError，避免静默产生 NaN。"""
    arr = np.asarray(v, dtype=float).reshape(-1)
    n = float(np.linalg.norm(arr))
    if n < eps:
        raise ValueError("Cannot normalize a near-zero vector.")
    return arr / n


def skew(v) -> np.ndarray:
    """向量叉乘矩阵：[v]x，使得 skew(v) @ x == v × x。"""
    arr = np.asarray(v, dtype=float).reshape(3)
    return np.array(
        [
            [0.0, -arr[2], arr[1]],
            [arr[2], 0.0, -arr[0]],
            [-arr[1], arr[0], 0.0],
        ],
        dtype=float,
    )


def make_tangent_basis(axis) -> Tuple[np.ndarray, np.ndarray]:
    """
    为单位轴向量构造稳定的切平面正交基 u, v。

    u ⟂ axis, v ⟂ axis, u ⟂ v，并且 u/v 都是单位向量。
    """
    a = normalize(axis)

    # 选择与 axis 最不平行的坐标轴作为参考，提升数值稳定性。
    abs_a = np.abs(a)
    if abs_a[0] <= abs_a[1] and abs_a[0] <= abs_a[2]:
        ref = np.array([1.0, 0.0, 0.0])
    elif abs_a[1] <= abs_a[2]:
        ref = np.array([0.0, 1.0, 0.0])
    else:
        ref = np.array([0.0, 0.0, 1.0])

    # Gram-Schmidt：从参考向量中剔除轴向分量，得到切平面方向 u。
    u = ref - np.dot(ref, a) * a
    u = normalize(u)

    # v 由右手系叉乘得到，天然与 a/u 正交。
    v = np.cross(a, u)
    v = normalize(v)
    return u, v


def angle_between_deg(a, b) -> float:
    """返回两个向量夹角，单位 degree。"""
    aa = normalize(a)
    bb = normalize(b)
    c = float(np.clip(np.dot(aa, bb), -1.0, 1.0))
    return float(np.degrees(np.arccos(c)))


def clamp_joint_limits(q, joint_limits) -> np.ndarray:
    """
    将关节角 clamp 到限位内。

    joint_limits 可以是 shape=(N, 2) 的数组，也可以为 None。
    """
    q_arr = np.asarray(q, dtype=float).reshape(-1)
    if joint_limits is None:
        return q_arr.copy()

    limits = np.asarray(joint_limits, dtype=float)
    if limits.shape != (q_arr.size, 2):
        raise ValueError(
            f"joint_limits must have shape ({q_arr.size}, 2), got {limits.shape}."
        )
    lower = limits[:, 0]
    upper = limits[:, 1]
    if np.any(lower > upper):
        raise ValueError("joint_limits lower bound must be <= upper bound.")
    return np.clip(q_arr, lower, upper)


def limit_dq(dq, dq_max: Optional[float]) -> np.ndarray:
    """按每个关节的单步最大增量限幅。"""
    dq_arr = np.asarray(dq, dtype=float).reshape(-1)
    if dq_max is None:
        return dq_arr.copy()
    if dq_max <= 0.0:
        raise ValueError("dq_max must be positive when provided.")
    return np.clip(dq_arr, -float(dq_max), float(dq_max))


def compute_pre_approach_position(
    target_position_base,
    approach_axis_base=np.array([0.0, 0.0, -1.0]),
    pre_distance: float = 0.05,
) -> np.ndarray:
    """
    target_position_base: 目标吸附点
    approach_axis_base: 末端接近方向，单位向量，例如 [0,0,-1]
    pre_distance: 预接近距离，默认 5cm

    返回：
    p_pre = target_position_base - approach_axis_base * pre_distance

    例如 approach_axis_base=[0,0,-1] 时，
    p_pre = target_position_base + [0,0,0.05]，即先到目标上方。
    """
    p = np.asarray(target_position_base, dtype=float).reshape(3)
    axis = normalize(approach_axis_base)
    if pre_distance < 0.0:
        raise ValueError("pre_distance must be non-negative.")
    return p - axis * float(pre_distance)


@dataclass
class IKDiagnostics:
    e_task: np.ndarray
    j_task: np.ndarray
    e_pos: np.ndarray
    e_ori_2d: np.ndarray
    suction_axis_current_base: np.ndarray
    tangent_u: np.ndarray
    tangent_v: np.ndarray


class IK5DSuctionSolver:
    def __init__(
        self,
        robot_model,
        joint_limits,
        tool_axis_local=np.array([0.0, 0.0, 1.0]),
        damping: float = 0.03,
        w_pos: float = 1.0,
        w_axis: float = 1.0,
        max_iter: int = 100,
        pos_tol: float = 0.003,
        axis_tol_deg: float = 5.0,
        dq_max: float = 0.05,
    ):
        self.robot_model = robot_model
        self.joint_limits = None if joint_limits is None else np.asarray(joint_limits, dtype=float)
        self.tool_axis_local = normalize(tool_axis_local)
        self.damping = float(damping)
        self.w_pos = float(w_pos)
        self.w_axis = float(w_axis)
        self.max_iter = int(max_iter)
        self.pos_tol = float(pos_tol)
        self.axis_tol_deg = float(axis_tol_deg)
        self.dq_max = float(dq_max)

        if self.damping < 0.0:
            raise ValueError("damping must be non-negative.")
        if self.max_iter <= 0:
            raise ValueError("max_iter must be positive.")
        if self.pos_tol < 0.0:
            raise ValueError("pos_tol must be non-negative.")
        if self.axis_tol_deg < 0.0:
            raise ValueError("axis_tol_deg must be non-negative.")
        if self.w_pos < 0.0 or self.w_axis < 0.0:
            raise ValueError("w_pos and w_axis must be non-negative.")
        if self.dq_max <= 0.0:
            raise ValueError("dq_max must be positive.")

    def _validate_q_and_limits(self, q) -> np.ndarray:
        q_arr = np.asarray(q, dtype=float).reshape(-1)
        if q_arr.size == 0:
            raise ValueError("q must contain at least one joint.")
        if self.joint_limits is not None and self.joint_limits.shape != (q_arr.size, 2):
            raise ValueError(
                f"joint_limits must have shape ({q_arr.size}, 2), "
                f"got {self.joint_limits.shape}."
            )
        return q_arr

    def _compute_task(self, q, target_position_base, target_axis_base) -> IKDiagnostics:
        q_arr = self._validate_q_and_limits(q)
        target_p = np.asarray(target_position_base, dtype=float).reshape(3)
        target_axis = normalize(target_axis_base)

        p_ee, r_ee = self.robot_model.fk(q_arr)
        j_pos, j_omega = self.robot_model.jacobian(q_arr)

        p_ee = np.asarray(p_ee, dtype=float).reshape(3)
        r_ee = np.asarray(r_ee, dtype=float)
        j_pos = np.asarray(j_pos, dtype=float)
        j_omega = np.asarray(j_omega, dtype=float)

        if r_ee.shape != (3, 3):
            raise ValueError(f"fk() must return R_ee with shape (3, 3), got {r_ee.shape}.")
        if j_pos.shape != (3, q_arr.size):
            raise ValueError(f"J_pos must have shape (3, {q_arr.size}), got {j_pos.shape}.")
        if j_omega.shape != (3, q_arr.size):
            raise ValueError(
                f"J_omega must have shape (3, {q_arr.size}), got {j_omega.shape}."
            )

        # 当前吸盘工具轴：suction_axis_current_base = R_ee @ tool_axis_local。
        suction_axis_current_base = normalize(r_ee @ self.tool_axis_local)

        # 位置误差：e_pos = target_position_base - p_ee，维度 3。
        e_pos = target_p - p_ee

        # 姿态方向误差：e_axis_3d = suction_axis_current_base × target_axis_base。
        # 该误差只描述两个轴的对齐，不包含绕吸盘轴自旋角。
        e_axis_3d = np.cross(suction_axis_current_base, target_axis)

        # 构造当前吸盘轴切平面基 u/v，并将 3D 方向误差投影成严格 2D 约束。
        u, v = make_tangent_basis(suction_axis_current_base)
        e_ori_2d = np.array([np.dot(e_axis_3d, u), np.dot(e_axis_3d, v)], dtype=float)

        # 组合 5D 任务误差：[3D 位置，2D 轴向姿态]。
        e_task = np.concatenate((self.w_pos * e_pos, self.w_axis * e_ori_2d))

        # 轴向雅可比：
        # d(axis)/dq = -skew(axis) @ J_omega，对应 axis × target 的小角度线性化。
        j_axis_3d = -skew(suction_axis_current_base) @ j_omega

        # 只保留切平面中的两个方向，严格丢弃绕吸盘轴的自旋自由度。
        j_axis_2d = np.vstack((u.T @ j_axis_3d, v.T @ j_axis_3d))

        # 组合 5xN 任务雅可比。
        j_task = np.vstack((self.w_pos * j_pos, self.w_axis * j_axis_2d))

        if e_task.shape != (5,):
            raise RuntimeError(f"Internal error: e_task must have shape (5,), got {e_task.shape}.")
        if j_task.shape != (5, q_arr.size):
            raise RuntimeError(
                f"Internal error: J_task must have shape (5, {q_arr.size}), got {j_task.shape}."
            )

        return IKDiagnostics(
            e_task=e_task,
            j_task=j_task,
            e_pos=e_pos,
            e_ori_2d=e_ori_2d,
            suction_axis_current_base=suction_axis_current_base,
            tangent_u=u,
            tangent_v=v,
        )

    def _dls_step(self, e_task: np.ndarray, j_task: np.ndarray) -> np.ndarray:
        # DLS: J_dls = J.T @ inv(J @ J.T + damping^2 * I_5)
        lhs = j_task @ j_task.T + (self.damping**2) * np.eye(5)
        try:
            y = np.linalg.solve(lhs, e_task)
        except np.linalg.LinAlgError:
            # 奇异或病态时使用 pinv 兜底，避免求解直接崩溃。
            y = np.linalg.pinv(lhs) @ e_task
        return j_task.T @ y

    def _final_errors(self, q, target_position_base, target_axis_base) -> Tuple[float, float]:
        p_ee, r_ee = self.robot_model.fk(q)
        p_ee = np.asarray(p_ee, dtype=float).reshape(3)
        r_ee = np.asarray(r_ee, dtype=float).reshape(3, 3)
        target_p = np.asarray(target_position_base, dtype=float).reshape(3)
        target_axis = normalize(target_axis_base)
        current_axis = normalize(r_ee @ self.tool_axis_local)
        pos_error = float(np.linalg.norm(target_p - p_ee))
        axis_error_deg = angle_between_deg(current_axis, target_axis)
        return pos_error, axis_error_deg

    def solve(
        self,
        q_init,
        target_position_base,
        target_axis_base=np.array([0.0, 0.0, -1.0]),
        verbose: bool = False,
    ) -> dict:
        """
        求解严格 5D IK，返回包含 success/failure_reason 和最终误差的 dict。
        """
        q = self._validate_q_and_limits(q_init)
        target_p = np.asarray(target_position_base, dtype=float).reshape(3)
        target_axis = normalize(target_axis_base)
        q = clamp_joint_limits(q, self.joint_limits)

        failure_reason = "max_iter_reached"
        iterations_done = 0

        for it in range(self.max_iter):
            iterations_done = it + 1
            try:
                diag = self._compute_task(q, target_p, target_axis)
            except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
                pos_error, axis_error_deg = self._safe_error_report(q, target_p, target_axis)
                return {
                    "success": False,
                    "q_solution": q,
                    "pos_error": pos_error,
                    "axis_error_deg": axis_error_deg,
                    "iterations": iterations_done - 1,
                    "failure_reason": f"model_or_input_error: {exc}",
                }

            pos_error = float(np.linalg.norm(diag.e_pos))
            axis_error_deg = angle_between_deg(diag.suction_axis_current_base, target_axis)

            if verbose:
                print(
                    f"iter={it:03d}, pos_error={pos_error:.6f}, "
                    f"axis_error_deg={axis_error_deg:.3f}"
                )

            if pos_error <= self.pos_tol and axis_error_deg <= self.axis_tol_deg:
                return {
                    "success": True,
                    "q_solution": q,
                    "pos_error": pos_error,
                    "axis_error_deg": axis_error_deg,
                    "iterations": iterations_done,
                    "failure_reason": "",
                }

            dq = self._dls_step(diag.e_task, diag.j_task)
            dq = limit_dq(dq, self.dq_max)

            if not np.all(np.isfinite(dq)):
                failure_reason = "non_finite_dq"
                break

            q_next = clamp_joint_limits(q + dq, self.joint_limits)
            if not np.all(np.isfinite(q_next)):
                failure_reason = "non_finite_q"
                break

            # 如果限位导致无法继续移动，提前报告停滞。
            if np.linalg.norm(q_next - q, ord=np.inf) < 1e-12:
                failure_reason = "stalled_by_joint_limits_or_zero_step"
                q = q_next
                break

            q = q_next

        pos_error, axis_error_deg = self._safe_error_report(q, target_p, target_axis)
        return {
            "success": False,
            "q_solution": q,
            "pos_error": pos_error,
            "axis_error_deg": axis_error_deg,
            "iterations": iterations_done,
            "failure_reason": failure_reason,
        }

    def _safe_error_report(self, q, target_position_base, target_axis_base) -> Tuple[float, float]:
        try:
            return self._final_errors(q, target_position_base, target_axis_base)
        except Exception:
            return float("inf"), float("inf")


class MockRobotModel:
    """
    简单 7 自由度链路模型，仅用于本文件自检。

    这是一个可替换的 FK/Jacobian 示例接口，不代表真实机械臂标定参数。
    关节轴按 z/y/y/x/y/x/y 交替布置，连杆沿局部 x 方向延伸。
    """

    def __init__(self, link_lengths: Optional[Sequence[float]] = None):
        self.link_lengths = np.asarray(
            link_lengths if link_lengths is not None else [0.10, 0.12, 0.12, 0.10, 0.08, 0.06, 0.04],
            dtype=float,
        ).reshape(-1)
        self.n = int(self.link_lengths.size)
        base_axes = [
            [0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ]
        self.local_axes = np.asarray(base_axes[: self.n], dtype=float)

    @staticmethod
    def _rot(axis, theta: float) -> np.ndarray:
        axis = normalize(axis)
        k = skew(axis)
        eye = np.eye(3)
        return eye + np.sin(theta) * k + (1.0 - np.cos(theta)) * (k @ k)

    def _forward_with_joint_frames(self, q):
        q_arr = np.asarray(q, dtype=float).reshape(-1)
        if q_arr.size != self.n:
            raise ValueError(f"MockRobotModel expects {self.n} joints, got {q_arr.size}.")

        p = np.zeros(3)
        r = np.eye(3)
        joint_origins = []
        joint_axes_base = []

        for i, theta in enumerate(q_arr):
            axis_base = r @ self.local_axes[i]
            joint_origins.append(p.copy())
            joint_axes_base.append(normalize(axis_base))
            r = r @ self._rot(self.local_axes[i], theta)
            p = p + r @ np.array([self.link_lengths[i], 0.0, 0.0])

        return p, r, joint_origins, joint_axes_base

    def fk(self, q):
        p, r, _, _ = self._forward_with_joint_frames(q)
        return p, r

    def jacobian(self, q):
        p_ee, _, joint_origins, joint_axes_base = self._forward_with_joint_frames(q)
        j_pos = np.zeros((3, self.n))
        j_omega = np.zeros((3, self.n))
        for i, (origin, axis) in enumerate(zip(joint_origins, joint_axes_base)):
            # Revolute joint geometric Jacobian:
            # 线速度列 = omega_i × (p_ee - p_joint_i)，角速度列 = omega_i。
            j_pos[:, i] = np.cross(axis, p_ee - origin)
            j_omega[:, i] = axis
        return j_pos, j_omega


def _assert_result_fields(result: dict) -> None:
    required = {
        "success",
        "q_solution",
        "pos_error",
        "axis_error_deg",
        "iterations",
        "failure_reason",
    }
    missing = required.difference(result.keys())
    if missing:
        raise AssertionError(f"solve() result missing fields: {sorted(missing)}")


if __name__ == "__main__":
    np.set_printoptions(precision=5, suppress=True)

    n = 7
    robot = MockRobotModel()
    joint_limits = np.tile(np.array([-np.pi, np.pi]), (n, 1))
    solver = IK5DSuctionSolver(
        robot_model=robot,
        joint_limits=joint_limits,
        tool_axis_local=np.array([0.0, 0.0, 1.0]),
        damping=0.03,
        w_pos=1.0,
        w_axis=1.0,
        max_iter=80,
        pos_tol=0.003,
        axis_tol_deg=5.0,
        dq_max=0.05,
    )

    q0 = np.zeros(n)
    target_position, target_rotation = robot.fk(q0)
    target_axis_not_unit = 3.0 * (target_rotation @ solver.tool_axis_local)

    diag = solver._compute_task(q0, target_position, target_axis_not_unit)
    assert diag.e_task.shape == (5,), f"e_task shape must be (5,), got {diag.e_task.shape}"
    assert diag.j_task.shape == (5, n), f"J_task shape must be (5, {n}), got {diag.j_task.shape}"

    normalized_axis = normalize(target_axis_not_unit)
    assert np.isclose(np.linalg.norm(normalized_axis), 1.0)
    assert np.allclose(normalized_axis, target_rotation @ solver.tool_axis_local)

    p_pre = compute_pre_approach_position(
        target_position_base=np.array([0.1, 0.2, 0.3]),
        approach_axis_base=np.array([0.0, 0.0, -1.0]),
        pre_distance=0.05,
    )
    assert np.allclose(p_pre, np.array([0.1, 0.2, 0.35]))

    result = solver.solve(q0, target_position, target_axis_not_unit, verbose=False)
    _assert_result_fields(result)

    print("5D suction IK self-test passed.")
    print(f"e_task shape: {diag.e_task.shape}")
    print(f"J_task shape: {diag.j_task.shape}")
    print(f"pre-approach: {p_pre}")
    print(
        "solve result: "
        f"success={result['success']}, pos_error={result['pos_error']:.6f}, "
        f"axis_error_deg={result['axis_error_deg']:.3f}, iterations={result['iterations']}, "
        f"failure_reason='{result['failure_reason']}'"
    )

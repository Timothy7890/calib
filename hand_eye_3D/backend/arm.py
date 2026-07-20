"""H2 手臂点动控制器（真机运动！），走官方 rt/arm_sdk 混合通道。

安全模型（与 hand_eye/backend/arm.py 一致）:
- 官方 arm_sdk 需要持续流式发令，后台线程以 50Hz 发送位置保持目标；
- 发出的目标 (cmd_q) 只会以受限速度向期望目标 (desired_q) 滑动，
  且始终钳制在 URDF 关节限位内 → 界面狂点也只会平滑慢速运动；
- 启动时先读当前实测姿态并从它开始保持，权重 1 秒内 0→1 渐入，不会跳变；
- 点动默认锁定，enable_jog() 后才接受目标；
- 卸力模式：被控手臂 kp=0、kd=小阻尼，人可以拖动（手臂会下坠，必须扶住），
  恢复点动时从人放置的位置重新抓取保持；
- 退出时权重 1 秒渐出交还本体控制器——退出前请扶住手臂。

只点动一条手臂（--arm），另一条手臂全程保持在启动时的实测姿态。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np

from .dds import ensure_dds_initialized
from .robot import (
    H2_LEFT_ARM_MOTOR_INDICES,
    H2_RIGHT_ARM_MOTOR_INDICES,
    IK_REPLAY_ROOT,
)

CONTROL_DT = 0.02          # 50Hz，官方示例节拍
WEIGHT_RAMP_S = 1.0        # 权重渐入/渐出时长
ARM_SDK_TOPIC = "rt/arm_sdk"
LOWSTATE_TOPIC = "rt/lowstate"
WEIGHT_MOTOR_INDEX = 31
DEFAULT_KP = 80.0
DEFAULT_KD = 1.5


def _load_joint_limits(arm: str) -> tuple[list[str], np.ndarray]:
    """从 IK_replay 的 h2 URDF 读该手臂 7 关节的名字和限位。"""
    if str(IK_REPLAY_ROOT) not in sys.path:
        sys.path.insert(0, str(IK_REPLAY_ROOT))
    from core.robot_config import load_robot_config
    from core.robot_model import RobotModel

    model = RobotModel(load_robot_config(IK_REPLAY_ROOT / "config" / "robots" / "h2.yaml"))
    chain = f"{arm}_arm"
    names = model.joint_names(chain)
    lower, upper = model.joint_limits(chain)
    return names, np.stack([lower, upper], axis=1)


class H2ArmController:
    """位置保持 + 限速点动 + 卸力拖动，发布 rt/arm_sdk（真机运动）。"""

    def __init__(self, arm: str = "right", network_interface: str | None = None,
                 max_speed_rad_s: float = 0.2, hand_move_kd: float = 2.0,
                 kp: float = DEFAULT_KP, kd: float = DEFAULT_KD,
                 lowstate_timeout: float = 5.0):
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        self.arm = arm
        self.joint_names, self.limits = _load_joint_limits(arm)
        self.n = len(self.joint_names)
        self.max_speed = float(max_speed_rad_s)
        self.hand_move_kd = float(hand_move_kd)
        self.kp = float(kp)
        self.kd = float(kd)

        self._jog_indices = (H2_RIGHT_ARM_MOTOR_INDICES if arm == "right"
                             else H2_LEFT_ARM_MOTOR_INDICES)
        self._other_indices = (H2_LEFT_ARM_MOTOR_INDICES if arm == "right"
                               else H2_RIGHT_ARM_MOTOR_INDICES)

        ensure_dds_initialized(network_interface)
        self._crc = CRC()
        self._low_cmd = unitree_hg_msg_dds__LowCmd_()
        self._publisher = ChannelPublisher(ARM_SDK_TOPIC, LowCmd_)
        self._publisher.Init()
        self._state_lock = threading.Lock()
        self._low_state = None
        self._subscriber = ChannelSubscriber(LOWSTATE_TOPIC, LowState_)
        self._subscriber.Init(self._on_low_state, 10)

        deadline = time.monotonic() + lowstate_timeout
        while self._low_state is None:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{lowstate_timeout:.0f}s 内没收到 {LOWSTATE_TOPIC}")
            time.sleep(0.05)

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._engaged = False
        self._jog_enabled = False
        self._float = False
        self._weight = 0.0

        q0 = self._clamp(self._read_motors(self._jog_indices))
        self._cmd_q = q0.copy()
        self._desired_q = q0.copy()
        self._other_hold_q = self._read_motors(self._other_indices)
        self._thread = threading.Thread(target=self._loop, name="h2-arm-jog", daemon=True)

    # ---- DDS 读 ----

    def _on_low_state(self, msg) -> None:
        with self._state_lock:
            self._low_state = msg

    def _read_motors(self, indices) -> np.ndarray:
        with self._state_lock:
            state = self._low_state
        if state is None:
            raise RuntimeError("还没收到 rt/lowstate")
        return np.asarray([state.motor_state[i].q for i in indices], dtype=float)

    def read_measured(self) -> np.ndarray:
        return self._read_motors(self._jog_indices)

    # ---- 生命周期 ----

    def start(self) -> None:
        """立即在当前实测姿态开始保持（点动仍锁定）。"""
        self._engaged = True
        self._thread.start()

    def shutdown(self) -> None:
        """权重渐出后停止发布。调用前请扶住手臂。"""
        self._stop_evt.set()
        self._thread.join(WEIGHT_RAMP_S + 1.0)

    # ---- 控制循环 ----

    def _clamp(self, q) -> np.ndarray:
        q = np.asarray(q, dtype=float).reshape(-1)
        return np.minimum(np.maximum(q, self.limits[:, 0]), self.limits[:, 1])

    def _write_command(self, jog_q: np.ndarray, float_mode: bool, weight: float) -> None:
        cmd = self._low_cmd
        cmd.motor_cmd[WEIGHT_MOTOR_INDEX].q = float(weight)
        for value, idx in zip(jog_q, self._jog_indices):
            m = cmd.motor_cmd[idx]
            m.tau = 0.0
            m.q = float(value)
            m.dq = 0.0
            if float_mode:
                m.kp = 0.0
                m.kd = self.hand_move_kd
            else:
                m.kp = self.kp
                m.kd = self.kd
        for value, idx in zip(self._other_hold_q, self._other_indices):
            m = cmd.motor_cmd[idx]
            m.tau = 0.0
            m.q = float(value)
            m.dq = 0.0
            m.kp = self.kp
            m.kd = self.kd
        cmd.crc = self._crc.Crc(cmd)
        self._publisher.Write(cmd)

    def _loop(self) -> None:
        next_t = time.perf_counter()
        while True:
            stopping = self._stop_evt.is_set()
            with self._lock:
                float_mode = self._float
                if stopping:
                    self._weight = max(0.0, self._weight - CONTROL_DT / WEIGHT_RAMP_S)
                else:
                    self._weight = min(1.0, self._weight + CONTROL_DT / WEIGHT_RAMP_S)
                weight = self._weight
                if not float_mode:
                    step = self.max_speed * CONTROL_DT
                    delta = np.clip(self._desired_q - self._cmd_q, -step, step)
                    self._cmd_q = self._cmd_q + delta
                cmd_q = self._cmd_q.copy()
            try:
                self._write_command(cmd_q, float_mode, weight)
            except Exception:
                pass
            if stopping and weight <= 0.0:
                break
            next_t += CONTROL_DT
            sleep = next_t - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.perf_counter()

    # ---- 控制操作（与 hand_eye 的 ArmController 同名同语义） ----

    def _safe_measured(self) -> np.ndarray | None:
        try:
            return self._clamp(self.read_measured())
        except Exception:
            return None

    def enter_hand_move(self) -> bool:
        """卸力拖动（真机会下坠，必须有人扶住）。仅点动关闭时允许。"""
        with self._lock:
            if self._jog_enabled:
                return False
            self._float = True
        return True

    def enable_jog(self) -> None:
        measured = self._safe_measured() if self._float else None
        with self._lock:
            if self._float and measured is not None:
                self._cmd_q = measured
            self._float = False
            self._desired_q = self._cmd_q.copy()
            self._jog_enabled = True

    def disable_jog(self) -> None:
        with self._lock:
            self._desired_q = self._cmd_q.copy()
            self._jog_enabled = False

    def stop(self) -> None:
        """冻结 + 刚性保持（也用于退出卸力模式）。"""
        measured = self._safe_measured() if self._float else None
        with self._lock:
            if self._float and measured is not None:
                self._cmd_q = measured
            self._float = False
            self._desired_q = self._cmd_q.copy()
            self._jog_enabled = False

    def set_target(self, q_desired) -> bool:
        with self._lock:
            if not self._jog_enabled:
                return False
            q = np.asarray(q_desired, dtype=float).reshape(-1)
            if q.size != self.n:
                raise ValueError(f"需要 {self.n} 个关节目标，收到 {q.size}")
            if not np.all(np.isfinite(q)):
                raise ValueError("目标包含非法值")
            self._desired_q = self._clamp(q)
            return True

    def nudge(self, index: int, delta: float) -> bool:
        with self._lock:
            if not self._jog_enabled:
                return False
            if not (0 <= index < self.n):
                raise IndexError(f"关节下标 {index} 越界")
            q = self._desired_q.copy()
            q[index] += float(delta)
            self._desired_q = self._clamp(q)
            return True

    def status(self) -> dict:
        with self._lock:
            cmd = self._cmd_q.copy()
            desired = self._desired_q.copy()
            jog = self._jog_enabled
            floating = self._float
            weight = self._weight
        try:
            measured = self.read_measured().tolist()
        except Exception:
            measured = None
        return {
            "arm": self.arm,
            "engaged": self._engaged,
            "jog_enabled": jog,
            "float": floating,
            "weight": weight,
            "joint_names": self.joint_names,
            "measured_rad": measured,
            "cmd_rad": cmd.tolist(),
            "desired_rad": desired.tolist(),
            "limits_rad": self.limits.tolist(),
            "max_speed_rad_s": self.max_speed,
        }

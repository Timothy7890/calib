"""
Minimal Unitree G1 right-arm joint executor.

This module is intentionally small and follows the official
unitree_sdk2_python/example/g1/low_level/g1_low_level_example.py pattern:
initialize DDS channels, read rt/lowstate, publish rt/lowcmd, set CRC.

It only commands the right arm joints used by the IK chain. Other motors are
held at their latest low_state positions in each LowCmd packet.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

import numpy as np


G1_NUM_MOTOR = 29

RIGHT_ARM_JOINT_NAMES = (
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_joint",
    "right_wrist_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
)

RIGHT_ARM_JOINT_TO_MOTOR_INDEX: Dict[str, int] = {
    "right_shoulder_pitch_joint": 22,
    "right_shoulder_roll_joint": 23,
    "right_shoulder_yaw_joint": 24,
    "right_elbow_joint": 25,
    "right_wrist_roll_joint": 26,
    "right_wrist_pitch_joint": 27,
    "right_wrist_yaw_joint": 28,
}

WAIST_MOTOR_INDICES = (12, 13, 14)


class Mode:
    PR = 0
    AB = 1


Kp = [
    60, 60, 60, 100, 40, 40,
    60, 60, 60, 100, 40, 40,
    60, 40, 40,
    40, 40, 40, 40, 40, 40, 40,
    250, 220, 150, 220, 40, 40, 40,
]

Kd = [
    1, 1, 1, 2, 1, 1,
    1, 1, 1, 2, 1, 1,
    1, 1, 1,
    1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1,
]


class OneShotJointErrorCompensator:
    def __init__(
        self,
        joint_names: Sequence[str],
        enabled: bool = False,
        comp_joint_names: Optional[Sequence[str]] = None,
        threshold_deg: float = 0.3,
        max_compensation_rad: float = 0.08,
        target_change_threshold: float = 0.003,
        target_stable_cycles: int = 150,
        qdot_threshold: float = 0.02,
        actual_stable_threshold: float = 0.002,
        joint_limits: Optional[Sequence[Sequence[float]]] = None,
        log_interval_cycles: int = 20,
    ):
        self.joint_names = tuple(joint_names)
        self.enabled = bool(enabled)
        self.comp_joint_names = set(comp_joint_names or self.joint_names)
        unknown = sorted(self.comp_joint_names.difference(self.joint_names))
        if unknown:
            raise ValueError(f"Unknown one-shot compensation joints: {unknown}")

        self.threshold_rad = math.radians(float(threshold_deg))
        self.threshold_deg = float(threshold_deg)
        self.max_compensation_rad = float(max_compensation_rad)
        self.target_change_threshold = float(target_change_threshold)
        self.target_stable_cycles = int(target_stable_cycles)
        self.qdot_threshold = float(qdot_threshold)
        self.actual_stable_threshold = float(actual_stable_threshold)
        self.log_interval_cycles = max(1, int(log_interval_cycles))
        self.joint_limits = None if joint_limits is None else np.asarray(joint_limits, dtype=float)
        if self.joint_limits is not None and self.joint_limits.shape != (len(self.joint_names), 2):
            raise ValueError(
                f"joint_limits must have shape ({len(self.joint_names)}, 2), "
                f"got {self.joint_limits.shape}."
            )

        n = len(self.joint_names)
        self.prev_target = np.full(n, np.nan, dtype=float)
        self.prev_actual = np.full(n, np.nan, dtype=float)
        self.stable_count = np.zeros(n, dtype=int)
        self.compensation_applied = np.zeros(n, dtype=bool)
        self.compensation_value = np.zeros(n, dtype=float)
        self.cycle = 0

    def reset(self, reason: str = "task_reset") -> None:
        if np.any(self.compensation_applied) or np.any(np.abs(self.compensation_value) > 0.0):
            print(f"[one_shot_joint_error_comp][RESET] reason={reason}")
        self.prev_target[:] = np.nan
        self.prev_actual[:] = np.nan
        self.stable_count[:] = 0
        self.compensation_applied[:] = False
        self.compensation_value[:] = 0.0

    def update(
        self,
        q_ik_target: Sequence[float],
        q_actual: Sequence[float],
        qdot_actual: Optional[Sequence[float]] = None,
        dt: Optional[float] = None,
    ) -> np.ndarray:
        q_target = np.asarray(q_ik_target, dtype=float).reshape(-1)
        q_act = np.asarray(q_actual, dtype=float).reshape(-1)
        if q_target.size != len(self.joint_names) or q_act.size != len(self.joint_names):
            raise ValueError("q_ik_target and q_actual must match joint_names length.")
        if not self.enabled:
            return q_target.copy()

        if qdot_actual is None:
            qdot = np.full_like(q_target, np.nan, dtype=float)
        else:
            qdot = np.asarray(qdot_actual, dtype=float).reshape(-1)
            if qdot.size != len(self.joint_names):
                raise ValueError("qdot_actual must match joint_names length.")

        q_cmd = q_target.copy()
        self.cycle += 1

        target_changed_flags = np.zeros(len(self.joint_names), dtype=bool)
        actual_stable_flags = np.zeros(len(self.joint_names), dtype=bool)
        for i, _ in enumerate(self.joint_names):
            target_changed = (
                not np.isfinite(self.prev_target[i])
                or abs(q_target[i] - self.prev_target[i]) >= self.target_change_threshold
            )
            target_changed_flags[i] = target_changed
            if target_changed:
                self.stable_count[i] = 0
            else:
                self.stable_count[i] += 1
            actual_stable_flags[i] = self._actual_is_stable(i, q_act[i], qdot[i], dt)

        if np.any(target_changed_flags) and (
            np.any(self.compensation_applied) or np.any(np.abs(self.compensation_value) > 0.0)
        ):
            changed = [self.joint_names[i] for i, changed_flag in enumerate(target_changed_flags) if changed_flag]
            print(f"[one_shot_joint_error_comp][RESET] reason=target_changed changed_joints={changed}")
            self.compensation_applied[:] = False
            self.compensation_value[:] = 0.0

        all_targets_stable = bool(np.all(self.stable_count >= self.target_stable_cycles))
        all_actual_stable = bool(np.all(actual_stable_flags))

        for i, name in enumerate(self.joint_names):
            if not np.isfinite(q_target[i]) or not np.isfinite(q_act[i]):
                self._reset_joint(i, "exception")
                q_cmd[i] = q_target[i] if np.isfinite(q_target[i]) else 0.0
                self.prev_target[i] = q_target[i] if np.isfinite(q_target[i]) else np.nan
                self.prev_actual[i] = q_act[i] if np.isfinite(q_act[i]) else np.nan
                continue

            if name not in self.comp_joint_names:
                self.prev_target[i] = q_target[i]
                self.prev_actual[i] = q_act[i]
                continue

            target_is_stable = all_targets_stable
            actual_is_stable = all_actual_stable
            raw_error = q_target[i] - q_act[i]

            if self.compensation_applied[i]:
                q_cmd[i] = q_target[i] + self.compensation_value[i]
            elif target_is_stable and actual_is_stable and abs(raw_error) > self.threshold_rad:
                comp = float(np.clip(raw_error, -self.max_compensation_rad, self.max_compensation_rad))
                self.compensation_value[i] = comp
                self.compensation_applied[i] = True
                q_cmd[i] = q_target[i] + comp
                print("[one_shot_joint_error_comp][APPLY]")
                print(f"  joint={name}")
                print(f"  q_ik_target={q_target[i]:.6f}")
                print(f"  q_actual={q_act[i]:.6f}")
                print(f"  raw_error_rad={raw_error:.6f}")
                print(f"  raw_error_deg={math.degrees(raw_error):.3f}")
                print(f"  compensation_value_rad={comp:.6f}")
                print(f"  compensation_value_deg={math.degrees(comp):.3f}")
                print(f"  q_cmd_after_comp={q_cmd[i]:.6f}")
                print(f"  all_7_target_stable={all_targets_stable}")
                print(f"  all_7_actual_stable={all_actual_stable}")

            q_cmd[i] = self._clamp_to_limits(i, name, q_cmd[i])

            if self.cycle % self.log_interval_cycles == 0:
                qdot_print = qdot[i] if np.isfinite(qdot[i]) else float("nan")
                print("[one_shot_joint_error_comp]")
                print(f"  joint={name}")
                print(f"  q_ik_target={q_target[i]:.6f}")
                print(f"  q_cmd={q_cmd[i]:.6f}")
                print(f"  q_actual={q_act[i]:.6f}")
                print(f"  raw_error={raw_error:.6f}")
                print(f"  raw_error_deg={math.degrees(raw_error):.3f}")
                print(f"  compensation_value={self.compensation_value[i]:.6f}")
                print(f"  compensation_value_deg={math.degrees(self.compensation_value[i]):.3f}")
                print(f"  compensation_applied={bool(self.compensation_applied[i])}")
                print(f"  stable_count={int(self.stable_count[i])}")
                print(f"  target_is_stable={target_is_stable}")
                print(f"  all_7_min_stable_count={int(np.min(self.stable_count))}")
                print(f"  all_7_target_stable={all_targets_stable}")
                print(f"  all_7_actual_stable={all_actual_stable}")
                print(f"  qdot_actual={qdot_print:.6f}")
                print(f"  threshold_deg={self.threshold_deg:.3f}")

            self.prev_target[i] = q_target[i]
            self.prev_actual[i] = q_act[i]

        return q_cmd

    def _actual_is_stable(self, i: int, q_actual: float, qdot_actual: float, dt: Optional[float]) -> bool:
        if np.isfinite(qdot_actual):
            return abs(float(qdot_actual)) <= self.qdot_threshold
        if np.isfinite(self.prev_actual[i]):
            delta = abs(float(q_actual) - float(self.prev_actual[i]))
            if dt is not None and dt > 0.0:
                return (delta / float(dt)) <= self.qdot_threshold
            return delta <= self.actual_stable_threshold
        return False

    def _clamp_to_limits(self, i: int, joint_name: str, q_value: float) -> float:
        if self.joint_limits is None:
            return float(q_value)
        lower, upper = self.joint_limits[i]
        q_clamped = float(np.clip(q_value, lower, upper))
        if q_clamped != float(q_value):
            print(
                "[one_shot_joint_error_comp][WARN] "
                f"joint={joint_name} q_cmd={q_value:.6f} exceeds limit "
                f"[{lower:.6f}, {upper:.6f}], clamped_to={q_clamped:.6f}"
            )
        return q_clamped

    def _reset_joint(self, i: int, reason: str) -> None:
        joint = self.joint_names[i]
        print(f"[one_shot_joint_error_comp][RESET] joint={joint} reason={reason}")
        self.stable_count[i] = 0
        self.compensation_applied[i] = False
        self.compensation_value[i] = 0.0


class UnitreeG1ArmExecutor:
    def __init__(
        self,
        network_interface: Optional[str] = None,
        wait_timeout: float = 5.0,
        command_rate_hz: float = 200.0,
        release_motion_mode: bool = True,
        joint_limits: Optional[Sequence[Sequence[float]]] = None,
        enable_one_shot_joint_error_comp: bool = False,
        one_shot_comp_joint_names: Optional[Sequence[str]] = None,
        one_shot_comp_threshold_deg: float = 0.3,
        one_shot_comp_max_rad: float = 0.08,
        one_shot_comp_target_change_threshold: float = 0.003,
        one_shot_comp_target_stable_cycles: int = 150,
        one_shot_comp_qdot_threshold: float = 0.02,
        one_shot_comp_actual_stable_threshold: float = 0.002,
        one_shot_comp_log_interval: int = 20,
    ):
        self.network_interface = network_interface
        self.wait_timeout = float(wait_timeout)
        self.command_rate_hz = float(command_rate_hz)
        self.low_state = None
        self.mode_machine = 0
        self.latest_commanded_q: Optional[np.ndarray] = None
        self.one_shot_compensator = OneShotJointErrorCompensator(
            RIGHT_ARM_JOINT_NAMES,
            enabled=enable_one_shot_joint_error_comp,
            comp_joint_names=one_shot_comp_joint_names,
            threshold_deg=one_shot_comp_threshold_deg,
            max_compensation_rad=one_shot_comp_max_rad,
            target_change_threshold=one_shot_comp_target_change_threshold,
            target_stable_cycles=one_shot_comp_target_stable_cycles,
            qdot_threshold=one_shot_comp_qdot_threshold,
            actual_stable_threshold=one_shot_comp_actual_stable_threshold,
            joint_limits=joint_limits,
            log_interval_cycles=one_shot_comp_log_interval,
        )

        repo_dir = Path(__file__).resolve().parent
        sdk_dir = repo_dir / "unitree_sdk2_python"
        if str(sdk_dir) not in sys.path:
            sys.path.insert(0, str(sdk_dir))

        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
        from unitree_sdk2py.utils.crc import CRC

        if network_interface:
            ChannelFactoryInitialize(0, network_interface)
        else:
            ChannelFactoryInitialize(0)

        if release_motion_mode:
            from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient

            msc = MotionSwitcherClient()
            msc.SetTimeout(5.0)
            msc.Init()
            status, result = msc.CheckMode()
            while isinstance(result, dict) and result.get("name"):
                print(f"[unitree_arm] releasing active motion mode: {result}")
                msc.ReleaseMode()
                time.sleep(1.0)
                status, result = msc.CheckMode()

        self.crc = CRC()
        self.low_cmd = unitree_hg_msg_dds__LowCmd_()
        self.lowcmd_publisher = ChannelPublisher("rt/lowcmd", LowCmd_)
        self.lowcmd_publisher.Init()
        self.lowstate_subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        self.lowstate_subscriber.Init(self._low_state_handler, 10)
        self.wait_for_low_state()

    def _low_state_handler(self, msg) -> None:
        self.low_state = msg
        self.mode_machine = msg.mode_machine

    def wait_for_low_state(self) -> None:
        deadline = time.time() + self.wait_timeout
        while self.low_state is None and time.time() < deadline:
            time.sleep(0.02)
        if self.low_state is None:
            raise RuntimeError("Timed out waiting for rt/lowstate.")

    def read_joint_positions(self, joint_names: Sequence[str] = RIGHT_ARM_JOINT_NAMES) -> np.ndarray:
        self.wait_for_low_state()
        return np.asarray(
            [self.low_state.motor_state[self._motor_index(name)].q for name in joint_names],
            dtype=float,
        )

    def reset_one_shot_compensation(self, reason: str = "task_reset") -> None:
        self.one_shot_compensator.reset(reason)

    def send_joint_target_once(
        self,
        joint_names: Sequence[str],
        q_target: Sequence[float],
        allow_one_shot_compensation: bool = True,
    ) -> None:
        self.wait_for_low_state()
        q_ik_target = np.asarray(q_target, dtype=float).reshape(-1)
        if q_ik_target.size != len(joint_names):
            raise ValueError(f"Expected {len(joint_names)} joint targets, got {q_ik_target.size}.")
        if not np.all(np.isfinite(q_ik_target)):
            raise ValueError("q_target contains NaN or Inf.")
        if allow_one_shot_compensation:
            q_actual = np.asarray(
                [self.low_state.motor_state[self._motor_index(name)].q for name in joint_names],
                dtype=float,
            )
            qdot_actual = np.asarray(
                [self.low_state.motor_state[self._motor_index(name)].dq for name in joint_names],
                dtype=float,
            )
            q_cmd = self.one_shot_compensator.update(
                q_ik_target,
                q_actual,
                qdot_actual=qdot_actual,
                dt=1.0 / self.command_rate_hz if self.command_rate_hz > 0.0 else None,
            )
        else:
            self.one_shot_compensator.reset("one_shot_disabled_for_motion_stage")
            q_cmd = q_ik_target.copy()

        self.low_cmd.mode_pr = Mode.PR
        self.low_cmd.mode_machine = self.mode_machine

        for i in range(G1_NUM_MOTOR):
            self.low_cmd.motor_cmd[i].mode = 1
            self.low_cmd.motor_cmd[i].tau = 0.0
            self.low_cmd.motor_cmd[i].q = self.low_state.motor_state[i].q
            self.low_cmd.motor_cmd[i].dq = 0.0
            self.low_cmd.motor_cmd[i].kp = Kp[i]
            self.low_cmd.motor_cmd[i].kd = Kd[i]

        for idx in WAIST_MOTOR_INDICES:
            self.low_cmd.motor_cmd[idx].q = 0.0
            self.low_cmd.motor_cmd[idx].dq = 0.0
            self.low_cmd.motor_cmd[idx].kp = Kp[idx]
            self.low_cmd.motor_cmd[idx].kd = Kd[idx]
            self.low_cmd.motor_cmd[idx].tau = 0.0

        for name, q_value in zip(joint_names, q_cmd):
            idx = self._motor_index(name)
            self.low_cmd.motor_cmd[idx].q = float(q_value)
            self.low_cmd.motor_cmd[idx].dq = 0.0
            self.low_cmd.motor_cmd[idx].kp = Kp[idx]
            self.low_cmd.motor_cmd[idx].kd = Kd[idx]
            self.low_cmd.motor_cmd[idx].tau = 0.0

        self.low_cmd.crc = self.crc.Crc(self.low_cmd)
        self.lowcmd_publisher.Write(self.low_cmd)

    def move_joint_target(
        self,
        joint_names: Sequence[str],
        q_target: Sequence[float],
        duration: float = 2.0,
    ) -> None:
        q_start = self.read_joint_positions(joint_names)
        q_goal = np.asarray(q_target, dtype=float).reshape(-1)
        if q_goal.size != len(joint_names):
            raise ValueError(f"Expected {len(joint_names)} joint targets, got {q_goal.size}.")
        if duration <= 0.0:
            raise ValueError("duration must be positive.")

        steps = max(2, int(round(float(duration) * self.command_rate_hz)))
        dt = 1.0 / self.command_rate_hz
        next_time = time.perf_counter()
        for i in range(1, steps + 1):
            alpha = i / float(steps)
            q_cmd = (1.0 - alpha) * q_start + alpha * q_goal
            self.send_joint_target_once(joint_names, q_cmd, allow_one_shot_compensation=False)
            next_time += dt
            sleep_time = next_time - time.perf_counter()
            if sleep_time > 0.0:
                time.sleep(sleep_time)
            else:
                next_time = time.perf_counter()
        self.latest_commanded_q = q_goal.copy()

    def execute_joint_trajectory(
        self,
        joint_names: Sequence[str],
        waypoints: Sequence[Sequence[float]],
        segment_durations: Sequence[float],
        start_q: Optional[Sequence[float]] = None,
        max_joint_speed: Optional[float] = 0.4,
        allow_one_shot_compensation: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """
        Continuously command a waypoint trajectory in one low-level send loop.

        Each segment is generated as a joint-space delta from the previous
        commanded waypoint, not by repeatedly re-reading lowstate. This keeps
        the arm under continuous rt/lowcmd control across pregrasp, approach,
        contact hold, and lift.
        """
        if len(waypoints) != len(segment_durations):
            raise ValueError("waypoints and segment_durations must have the same length.")
        if not waypoints:
            return

        if start_q is None:
            q_prev = self.read_joint_positions(joint_names)
        else:
            q_prev = np.asarray(start_q, dtype=float).reshape(-1)
        if q_prev.size != len(joint_names):
            raise ValueError(f"Expected start_q with {len(joint_names)} joints, got {q_prev.size}.")
        if not np.all(np.isfinite(q_prev)):
            raise ValueError("start_q contains NaN or Inf.")

        dt = 1.0 / self.command_rate_hz
        next_time = time.perf_counter()
        for segment_index, (waypoint, duration) in enumerate(zip(waypoints, segment_durations), start=1):
            if cancel_check is not None and cancel_check():
                raise InterruptedError("Joint trajectory canceled before segment start.")
            q_next = np.asarray(waypoint, dtype=float).reshape(-1)
            if q_next.size != len(joint_names):
                raise ValueError(
                    f"Segment {segment_index}: expected {len(joint_names)} joints, got {q_next.size}."
                )
            if not np.all(np.isfinite(q_next)):
                raise ValueError(f"Segment {segment_index}: waypoint contains NaN or Inf.")
            if duration <= 0.0:
                raise ValueError(f"Segment {segment_index}: duration must be positive.")

            delta = q_next - q_prev
            duration_eff = float(duration)
            if max_joint_speed is not None:
                if max_joint_speed <= 0.0:
                    raise ValueError("max_joint_speed must be positive when provided.")
                duration_eff = max(duration_eff, float(np.max(np.abs(delta))) / float(max_joint_speed))

            steps = max(2, int(round(duration_eff * self.command_rate_hz)))
            for i in range(1, steps + 1):
                if cancel_check is not None and cancel_check():
                    raise InterruptedError("Joint trajectory canceled.")
                u = i / float(steps)
                alpha = u * u * (3.0 - 2.0 * u)
                q_cmd = q_prev + alpha * delta
                self.send_joint_target_once(
                    joint_names,
                    q_cmd,
                    allow_one_shot_compensation=allow_one_shot_compensation,
                )
                self.latest_commanded_q = q_cmd.copy()

                next_time += dt
                sleep_time = next_time - time.perf_counter()
                if sleep_time > 0.0:
                    time.sleep(sleep_time)
                else:
                    next_time = time.perf_counter()
            q_prev = q_next.copy()

    def hold_joint_target(
        self,
        joint_names: Sequence[str],
        q_target: Sequence[float],
        hold_seconds: float = 0.5,
        allow_one_shot_compensation: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        end_time = time.time() + float(hold_seconds)
        dt = 1.0 / self.command_rate_hz
        while time.time() < end_time:
            if cancel_check is not None and cancel_check():
                raise InterruptedError("Joint hold canceled.")
            self.send_joint_target_once(
                joint_names,
                q_target,
                allow_one_shot_compensation=allow_one_shot_compensation,
            )
            self.latest_commanded_q = np.asarray(q_target, dtype=float).reshape(-1).copy()
            time.sleep(dt)

    def _motor_index(self, joint_name: str) -> int:
        if joint_name not in RIGHT_ARM_JOINT_TO_MOTOR_INDEX:
            raise KeyError(f"No right-arm motor mapping for joint {joint_name!r}.")
        return RIGHT_ARM_JOINT_TO_MOTOR_INDEX[joint_name]

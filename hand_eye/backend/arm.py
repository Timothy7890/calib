"""Right-arm jog controller for hand-eye posing (REAL-ROBOT MOTION).

SAFETY MODEL
------------
This commands the physical right arm. Unitree low-level requires *continuous*
streaming to hold a pose, so a background thread sends a position-hold target at
``command_rate_hz``. The streamed command (``cmd_q``) only ever *slews* toward the
user's desired target (``desired_q``) at a bounded joint speed, and every target is
clamped to URDF joint limits. Therefore:

- Dragging a slider fast still moves the arm slowly and smoothly.
- ``start()`` begins holding at the CURRENT measured pose immediately, so the arm
  never sags after the executor releases the high-level motion mode.
- Jog is locked until ``enable_jog()``. ``disable_jog()`` freezes + keeps holding.
- The thread keeps the arm energized while the server runs; SUPPORT THE ARM
  before shutting the server down (streaming stops on exit).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# scripts/handeye/backend/arm.py -> [0]=backend [1]=handeye [2]=scripts [3]=repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class ArmController:
    """Background position-hold + slew-rate-limited jog for the right arm."""

    def __init__(
        self,
        network_interface: Optional[str] = None,
        limits: Optional[Dict[str, Tuple[float, float]]] = None,
        max_speed_rad_s: float = 0.2,
        command_rate_hz: float = 200.0,
        hand_move_kd: float = 2.0,
    ):
        from unitree_g1_arm_executor import RIGHT_ARM_JOINT_NAMES, UnitreeG1ArmExecutor

        self.joint_names: List[str] = list(RIGHT_ARM_JOINT_NAMES)
        self.n = len(self.joint_names)
        self.source = "inproc(arm)"
        self.max_speed = float(max_speed_rad_s)
        self.command_rate_hz = float(command_rate_hz)
        self._dt = 1.0 / self.command_rate_hz
        self.hand_move_kd = float(hand_move_kd)

        lim = np.tile(np.array([-np.pi, np.pi], dtype=float), (self.n, 1))
        if limits:
            for i, name in enumerate(self.joint_names):
                rng = limits.get(name)
                if rng is not None:
                    lim[i] = [float(rng[0]), float(rng[1])]
        self.limits = lim

        # release_motion_mode=True so low-level commands take effect.
        self._executor = UnitreeG1ArmExecutor(
            network_interface=network_interface,
            release_motion_mode=True,
            command_rate_hz=self.command_rate_hz,
        )

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._engaged = False
        self._jog_enabled = False
        self._float = False  # hand-guide (compliant) mode: arm goes soft

        q0 = self._clamp(self._executor.read_joint_positions(self.joint_names))
        self._cmd_q = q0.copy()
        self._desired_q = q0.copy()
        self._thread = threading.Thread(target=self._loop, name="arm-jog", daemon=True)

    # ----- lifecycle -----

    def start(self) -> None:
        """Begin holding immediately at the current pose (jog stays locked)."""
        self._engaged = True
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    # ----- helpers -----

    def _clamp(self, q) -> np.ndarray:
        q = np.asarray(q, dtype=float).reshape(-1)
        return np.minimum(np.maximum(q, self.limits[:, 0]), self.limits[:, 1])

    def _loop(self) -> None:
        next_t = time.perf_counter()
        while not self._stop.is_set():
            if self._engaged:
                with self._lock:
                    float_mode = self._float
                    if not float_mode:
                        step = self.max_speed * self._dt
                        delta = np.clip(self._desired_q - self._cmd_q, -step, step)
                        self._cmd_q = self._cmd_q + delta
                        cmd = self._cmd_q.copy()
                try:
                    if float_mode:
                        self._executor.send_compliant_arm_once(
                            self.joint_names, kd=self.hand_move_kd
                        )
                    else:
                        self._executor.send_joint_target_once(
                            self.joint_names, cmd, allow_one_shot_compensation=False
                        )
                except Exception:
                    pass
            next_t += self._dt
            sleep_time = next_t - time.perf_counter()
            if sleep_time > 0.0:
                time.sleep(sleep_time)
            else:
                next_t = time.perf_counter()

    # ----- control ops -----

    def _safe_measured(self) -> Optional[np.ndarray]:
        try:
            return self._clamp(self.read_measured())
        except Exception:
            return None

    def enter_hand_move(self) -> bool:
        """Make the right arm COMPLIANT for hand guiding. Only allowed when jog
        is off. The arm WILL sag (no gravity comp) — the operator must hold it."""
        with self._lock:
            if self._jog_enabled:
                return False
            self._float = True
        return True

    def enable_jog(self) -> None:
        # If we were hand-guiding, re-grab the pose where the user left the arm so
        # holding/jog resumes from there with no jump.
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
        # Freeze + rigid-hold here; also exits hand-guide mode.
        measured = self._safe_measured() if self._float else None
        with self._lock:
            if self._float and measured is not None:
                self._cmd_q = measured
            self._float = False
            self._desired_q = self._cmd_q.copy()
            self._jog_enabled = False

    def set_target(self, q_desired: Sequence[float]) -> bool:
        with self._lock:
            if not self._jog_enabled:
                return False
            q = np.asarray(q_desired, dtype=float).reshape(-1)
            if q.size != self.n:
                raise ValueError(f"Expected {self.n} joint targets, got {q.size}.")
            if not np.all(np.isfinite(q)):
                raise ValueError("Target contains non-finite values.")
            self._desired_q = self._clamp(q)
            return True

    def nudge(self, index: int, delta: float) -> bool:
        with self._lock:
            if not self._jog_enabled:
                return False
            if index < 0 or index >= self.n:
                raise IndexError(f"joint index {index} out of range")
            q = self._desired_q.copy()
            q[index] += float(delta)
            self._desired_q = self._clamp(q)
            return True

    # ----- readouts -----

    def read_measured(self) -> np.ndarray:
        return self._executor.read_joint_positions(self.joint_names)

    def status(self) -> dict:
        with self._lock:
            cmd = self._cmd_q.copy()
            desired = self._desired_q.copy()
            engaged = self._engaged
            jog = self._jog_enabled
            floating = self._float
        try:
            measured = self.read_measured().tolist()
        except Exception as exc:
            measured = None
        return {
            "engaged": engaged,
            "jog_enabled": jog,
            "float": floating,
            "joint_names": self.joint_names,
            "measured_rad": measured,
            "cmd_rad": cmd.tolist(),
            "desired_rad": desired.tolist(),
            "limits_rad": self.limits.tolist(),
            "max_speed_rad_s": self.max_speed,
        }


class ControllerJointProvider:
    """JointProvider-compatible reader backed by an ArmController (single executor)."""

    def __init__(self, controller: ArmController):
        self._controller = controller
        self.joint_names = controller.joint_names
        self.source = "inproc(arm)"

    def read(self) -> np.ndarray:
        return self._controller.read_measured()

    def close(self) -> None:
        pass

"""Pluggable joint-angle providers for hand-eye capture.

The capture tool only READS joint encoders; it never commands the arm.

Three sources keep us robust to the unknown runtime environment:
  - inproc : read rt/lowstate directly via UnitreeG1ArmExecutor (release_motion_mode=False)
  - http   : GET a small JSON endpoint provided by a separate robot-side process
  - mock   : constant zeros, for exercising the web UI without the robot
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

# Right-arm chain joints, in the order used by the IK/URDF model.
HANDEYE_JOINT_NAMES: List[str] = [
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_joint",
    "right_wrist_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
]

# scripts/handeye/backend/joints.py -> parents: [0]=backend [1]=handeye [2]=scripts [3]=vision_arm_control
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = Path(__file__).resolve().parents[2]


class JointProvider:
    """Interface: read() returns the current joint vector, or raises on failure."""

    joint_names: List[str] = HANDEYE_JOINT_NAMES
    source: str = "base"

    def read(self) -> np.ndarray:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MockJointProvider(JointProvider):
    source = "mock"

    def read(self) -> np.ndarray:
        return np.zeros(len(self.joint_names), dtype=float)


class HttpJointProvider(JointProvider):
    """Reads joints from a JSON endpoint: {"q": [...], "joint_names": [...] (optional)}."""

    source = "http"

    def __init__(self, url: str, timeout: float = 2.0):
        self.url = url
        self.timeout = float(timeout)

    def read(self) -> np.ndarray:
        request = urllib.request.Request(self.url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
        data = json.loads(payload)
        if "joint_names" in data and data["joint_names"]:
            self.joint_names = list(data["joint_names"])
        q = np.asarray(data["q"], dtype=float).reshape(-1)
        if q.size != len(self.joint_names):
            raise ValueError(
                f"HTTP joints length {q.size} != joint_names length {len(self.joint_names)}"
            )
        if not np.all(np.isfinite(q)):
            raise ValueError(f"HTTP joints contain non-finite values: {q}")
        return q


class InProcessJointProvider(JointProvider):
    """Reads rt/lowstate via UnitreeG1ArmExecutor, constructed read-only.

    release_motion_mode=False is used so constructing the executor does NOT
    change the robot's active control mode, and no command is ever sent.
    """

    source = "inproc"

    def __init__(self, network_interface: Optional[str] = None):
        for p in (str(_REPO_ROOT), str(_SCRIPTS_DIR)):
            if p not in sys.path:
                sys.path.insert(0, p)
        from unitree_g1_arm_executor import RIGHT_ARM_JOINT_NAMES, UnitreeG1ArmExecutor

        self.joint_names = list(RIGHT_ARM_JOINT_NAMES)
        self._executor = UnitreeG1ArmExecutor(
            network_interface=network_interface,
            release_motion_mode=False,
        )

    def read(self) -> np.ndarray:
        return self._executor.read_joint_positions(self.joint_names)


def make_joint_provider(
    source: str,
    *,
    network_interface: Optional[str] = None,
    http_url: Optional[str] = None,
) -> JointProvider:
    if source == "mock":
        return MockJointProvider()
    if source == "http":
        if not http_url:
            raise ValueError("joint source 'http' requires --joints-url.")
        return HttpJointProvider(http_url)
    if source == "inproc":
        return InProcessJointProvider(network_interface=network_interface)
    raise ValueError(f"Unknown joint source: {source!r} (use 'inproc', 'http', or 'mock').")

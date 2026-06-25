#!/usr/bin/env python3
"""Body-side joint sidecar: read rt/lowstate locally, expose right-arm joints over HTTP.

WHY
---
When the control machine is behind a router/switch (not a direct cable), DDS
multicast discovery is dropped and the control machine cannot subscribe to
rt/lowstate. This sidecar runs ON THE ROBOT BODY, where reading lowstate works
locally, and serves the right-arm joint vector over plain HTTP (TCP), which the
control machine pulls via the hand-eye server's ``--joint-source http``.

DEPENDENCIES
------------
Only the Python standard library + the robot's own ``unitree_sdk2py`` (+ cyclonedds),
which are already installed on the body. No FastAPI/numpy needed.

RUN (on the body)
-----------------
    ssh unitree@192.168.123.164
    # use whatever python env has unitree_sdk2py:
    python3 joint_sidecar.py --port 18090
    # if unitree_sdk2py is not importable, point to it:
    python3 joint_sidecar.py --port 18090 --sdk-path /path/to/unitree_sdk2_python

ENDPOINT
--------
    GET /joints -> {"q": [7 floats rad], "joint_names": [...], "ok": true, "age_s": ...}
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Right-arm joints and their G1 motor indices (matches unitree_g1_arm_executor).
RIGHT_ARM = [
    ("right_shoulder_pitch_joint", 22),
    ("right_shoulder_roll_joint", 23),
    ("right_shoulder_yaw_joint", 24),
    ("right_elbow_joint", 25),
    ("right_wrist_roll_joint", 26),
    ("right_wrist_pitch_joint", 27),
    ("right_wrist_yaw_joint", 28),
]
JOINT_NAMES = [name for name, _ in RIGHT_ARM]
MOTOR_INDICES = [idx for _, idx in RIGHT_ARM]

_state = {"low_state": None, "stamp": 0.0}
_lock = threading.Lock()


def _on_lowstate(msg) -> None:
    with _lock:
        _state["low_state"] = msg
        _state["stamp"] = time.time()


def read_q():
    with _lock:
        ls = _state["low_state"]
        stamp = _state["stamp"]
    if ls is None:
        return None, None
    q = [float(ls.motor_state[idx].q) for idx in MOTOR_INDICES]
    return q, stamp


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/joints", "/"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        q, stamp = read_q()
        if q is None:
            self._send(503, {"ok": False, "error": "no lowstate yet"})
            return
        self._send(200, {
            "ok": True,
            "q": q,
            "joint_names": JOINT_NAMES,
            "age_s": round(time.time() - stamp, 3),
        })

    def log_message(self, *args):
        pass  # quiet


def main() -> int:
    parser = argparse.ArgumentParser(description="Body-side right-arm joint HTTP sidecar")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=18090, help="HTTP port")
    parser.add_argument("--iface", default=None, help="DDS interface (default: auto-determine)")
    parser.add_argument("--domain", type=int, default=0, help="DDS domain id")
    parser.add_argument("--sdk-path", default=None, help="Path to unitree_sdk2_python if not importable")
    args = parser.parse_args()

    if args.sdk_path:
        sys.path.insert(0, args.sdk_path)

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

    if args.iface:
        print(f"[sidecar] ChannelFactoryInitialize(domain={args.domain}, iface={args.iface})")
        ChannelFactoryInitialize(args.domain, args.iface)
    else:
        print(f"[sidecar] ChannelFactoryInitialize(domain={args.domain}, auto-determine iface)")
        ChannelFactoryInitialize(args.domain)

    sub = ChannelSubscriber("rt/lowstate", LowState_)
    sub.Init(_on_lowstate, 10)

    # Wait briefly for first sample so the first HTTP hit isn't a 503.
    deadline = time.time() + 5.0
    while time.time() < deadline and _state["low_state"] is None:
        time.sleep(0.05)
    if _state["low_state"] is None:
        print("[sidecar] WARNING: no lowstate received in 5s. Is the body publishing it? "
              "Try --iface <body-iface> or a different --domain.")
    else:
        q, _ = read_q()
        print(f"[sidecar] lowstate OK. right-arm q = {q}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[sidecar] serving GET /joints on http://{args.host}:{args.port}")
    print("[sidecar] from the control machine: "
          f"--joint-source http --joints-url http://<body-ip>:{args.port}/joints")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

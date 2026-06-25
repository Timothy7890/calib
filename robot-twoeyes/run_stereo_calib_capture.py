#!/usr/bin/env python3
"""Standalone stereo-calibration image capture server (no calib yaml required).

Why this exists
---------------
robot-twoeyes' depth/run_server.py mounts the calibration capture UI at
``/calibrate`` but REQUIRES an existing ``stereo_calibration.yaml`` (for its depth
feature). For a FIRST-TIME calibration on a fresh robot you do not have that yaml
yet. This launcher mounts ONLY the calibration capture sub-app at ``/calibrate`` --
so the existing Vue ``/calibrate`` page works unchanged -- without needing any yaml.

Run it from the control machine; images are pulled from the head camera over the
network via teleimager (``--camera_host`` = robot body IP).

Prerequisites
-------------
- Python 3.9 env (this backend uses ``tuple[int, int]`` annotations).
- teleimager on PYTHONPATH, e.g. ``export PYTHONPATH=$HOME/teleimager_src_copy:$PYTHONPATH``.
- Network reachability to the body (TCP 60000 + head-camera zmq_port).

Example
-------
    export PYTHONPATH=$HOME/teleimager_src_copy:$PYTHONPATH
    # from the robot-twoeyes project root:
    python run_stereo_calib_capture.py \
        --camera_host 192.168.123.164 --board_size 11x8 --port 8124
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# This script now lives at the robot-twoeyes project root, so the repo root is
# simply its own directory (``import backend.main`` resolves from here).
RT_ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Stereo calibration capture (no yaml needed)")
    parser.add_argument("--save_path", default=str(RT_ROOT / "data" / "calib_images"),
                        help="Base directory for calibration captures")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind address")
    parser.add_argument("--port", type=int, default=8124, help="Server port (frontend proxies to 8124)")
    parser.add_argument("--camera_host", default="192.168.123.164", help="Teleimager camera host (robot body IP)")
    parser.add_argument("--board_size", default="11x8", help="Chessboard inner corners, e.g. 11x8")
    parser.add_argument("--no-timestamp-dir", action="store_true",
                        help="Save directly into --save_path instead of a timestamped subfolder")
    args = parser.parse_args()

    if not (RT_ROOT / "backend").exists():
        raise SystemExit(f"robot-twoeyes backend not found next to this script ({RT_ROOT})")
    sys.path.insert(0, str(RT_ROOT))

    os.environ["CAMERA_HOST"] = args.camera_host

    import uvicorn
    from fastapi import FastAPI

    import backend.main as calib
    from backend.camera import CameraManager
    from backend.detection import parse_board_size

    session_dir = Path(args.save_path)
    if not args.no_timestamp_dir:
        session_dir = session_dir / datetime.now().strftime("%Y%m%d%H%M%S")

    # Replicate the sub-app's lifespan setup manually, because a mounted
    # sub-app's lifespan handler does not run automatically.
    calib.save_path = session_dir
    calib.board_size = parse_board_size(args.board_size)
    calib.camera = CameraManager(host=args.camera_host)
    (session_dir / "left").mkdir(parents=True, exist_ok=True)
    (session_dir / "right").mkdir(parents=True, exist_ok=True)
    calib.capture_count = calib._count_existing_captures()

    app = FastAPI(title="Stereo Calib Capture")
    app.mount("/calibrate", calib.app)

    print(f"[calib] camera_host = {args.camera_host}")
    print(f"[calib] board_size  = {args.board_size}")
    print(f"[calib] save_path   = {session_dir}")
    print(f"[calib] existing captures = {calib.capture_count}")
    print(f"[calib] serving /calibrate on http://{args.host}:{args.port}")
    print("[calib] open the frontend (npx vite --host) at  http://<control-ip>:7009/calibrate")

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

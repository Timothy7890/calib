#!/usr/bin/env python3
"""Entry point for the depth capture web server."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Depth Capture Web Server")
    parser.add_argument("--calib_path", required=True, help="Path to stereo_calibration.yaml")
    parser.add_argument("--save_path", default="./data/depth_captures", help="Base directory for captures")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind address")
    parser.add_argument("--port", type=int, default=8124, help="Server port")
    parser.add_argument("--camera_host", default="127.0.0.1", help="Teleimager camera host")
    parser.add_argument("--calib_save_path", default="./data/calib_images",
                        help="Base directory for calibration captures")
    parser.add_argument("--board_size", default="11x8", help="Chessboard inner corners, e.g. 11x8")
    parser.add_argument("--disparity-method", default="sgbm", choices=["sgbm", "crestereo"],
                        help="Disparity computation method")
    parser.add_argument("--no-wls", action="store_true", help="Disable WLS filtering by default")
    args = parser.parse_args()

    os.environ["CAMERA_HOST"] = args.camera_host

    import depth.main as app_module
    import backend.main as calib_module
    from depth.stereo_depth import StereoDepthProcessor
    from backend.detection import parse_board_size

    timestamp_dir = datetime.now().strftime("%Y%m%d%H%M")
    app_module.save_path = Path(args.save_path) / timestamp_dir
    app_module.processor = StereoDepthProcessor(args.calib_path, method=args.disparity_method)
    app_module.use_wls = not args.no_wls
    app_module.disparity_method = args.disparity_method

    # Configure calibration sub-app
    calib_save = Path(args.calib_save_path) / timestamp_dir
    calib_module.save_path = calib_save
    (calib_save / "left").mkdir(parents=True, exist_ok=True)
    (calib_save / "right").mkdir(parents=True, exist_ok=True)
    calib_module.board_size = parse_board_size(args.board_size)

    import uvicorn
    uvicorn.run(app_module.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

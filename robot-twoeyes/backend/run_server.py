#!/usr/bin/env python3
"""Convenience entry point for the stereo calibration web server."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Stereo Calibration Web Server")
    parser.add_argument("--save_path", default="./calib_images", help="Base directory to save captured images")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind address")
    parser.add_argument("--port", type=int, default=8123, help="Server port")
    parser.add_argument("--camera_host", default="127.0.0.1", help="Teleimager camera host")
    parser.add_argument("--board_size", default="11x8", help="Chessboard inner corners, e.g. 11x8")
    args = parser.parse_args()

    os.environ["CAMERA_HOST"] = args.camera_host

    import backend.main as app_module
    from backend.detection import parse_board_size

    timestamp_dir = datetime.now().strftime("%Y%m%d%H%M")
    app_module.save_path = Path(args.save_path) / timestamp_dir
    app_module.board_size = parse_board_size(args.board_size)

    import uvicorn
    uvicorn.run(app_module.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

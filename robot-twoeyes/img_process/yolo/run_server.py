#!/usr/bin/env python3
"""Entry point for the YOLO detection API server."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def main():
    parser = argparse.ArgumentParser(description="YOLO11 Detection API Server")
    parser.add_argument("--model", required=True, help="Path to YOLO11 .onnx model file")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind address")
    parser.add_argument("--port", type=int, default=8125, help="Server port")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold")
    parser.add_argument("--class-names", nargs="+", default=None,
                        help="Custom class names, e.g. --class-names panda")
    parser.add_argument("--no-gpu", action="store_true", help="Disable GPU, use CPU only")
    args = parser.parse_args()

    model_file = Path(args.model)
    if not model_file.exists():
        print(f"[ERROR] Model file not found: {model_file}")
        sys.exit(1)

    import os
    os.environ["YOLO_USE_GPU"] = "0" if args.no_gpu else "1"

    import img_process.yolo.main as app_module

    app_module.model_path = str(model_file)
    app_module.conf_threshold = args.conf
    app_module.iou_threshold = args.iou
    app_module.class_names = args.class_names

    import uvicorn
    print(f"Starting YOLO server on {args.host}:{args.port}")
    print(f"  Model : {model_file}")
    print(f"  GPU   : {'OFF' if args.no_gpu else 'ON'}")
    print(f"  Conf  : {args.conf}")
    print(f"  IoU   : {args.iou}")
    uvicorn.run(app_module.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

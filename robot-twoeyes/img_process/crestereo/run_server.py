#!/usr/bin/env python3
"""Entry point for the CREStereo disparity estimation API server."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def main():
    parser = argparse.ArgumentParser(description="CREStereo Disparity API Server")
    parser.add_argument("--model", required=True,
                        help="Path to CREStereo .onnx model (init or combined)")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind address")
    parser.add_argument("--port", type=int, default=8126, help="Server port")
    parser.add_argument("--no-gpu", action="store_true", help="Disable GPU")
    args = parser.parse_args()

    model_file = Path(args.model)
    if not model_file.exists():
        print(f"[ERROR] Model file not found: {model_file}")
        sys.exit(1)

    import os
    os.environ["CRE_USE_GPU"] = "0" if args.no_gpu else "1"

    import img_process.crestereo.main as app_module

    app_module.model_path = str(model_file)

    import uvicorn
    print(f"Starting CREStereo server on {args.host}:{args.port}")
    print(f"  Model : {model_file}")
    print(f"  GPU   : {'OFF' if args.no_gpu else 'ON'}")
    uvicorn.run(app_module.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

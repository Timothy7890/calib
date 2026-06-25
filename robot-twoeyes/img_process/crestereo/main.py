"""FastAPI application for CREStereo stereo disparity estimation."""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from img_process.crestereo.estimator import CREStereoEstimator

# --------------- Global State ---------------

estimator: Optional[CREStereoEstimator] = None
model_path: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global estimator
    providers = None
    use_gpu = os.environ.get("CRE_USE_GPU", "1") == "1"
    if use_gpu:
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    estimator = CREStereoEstimator(model_path, providers=providers)
    dummy_l = np.zeros((480, 640, 3), dtype=np.uint8)
    dummy_r = np.zeros((480, 640, 3), dtype=np.uint8)
    estimator.estimate(dummy_l, dummy_r)
    print(f"[CREStereo] Warmup complete (inference: {estimator.inf_time:.3f}s), ready to serve.")
    yield
    estimator = None


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------- Helpers ---------------


def decode_image(b64: str) -> np.ndarray:
    data = base64.b64decode(b64)
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def encode_array(arr: np.ndarray) -> str:
    """Encode a numpy float32 array as base64 raw bytes."""
    return base64.b64encode(arr.astype(np.float32).tobytes()).decode("ascii")


# --------------- API ---------------


@app.post("/api/disparity")
async def api_disparity(payload: dict):
    """Compute disparity from a rectified stereo pair.

    Request body:
        {
            "left": "<base64 JPEG>",
            "right": "<base64 JPEG>"
        }

    Response:
        {
            "disparity": "<base64 float32 raw bytes>",
            "height": int,
            "width": int,
            "inference_time": float
        }
    """
    left_b64 = payload.get("left", "")
    right_b64 = payload.get("right", "")
    if not left_b64 or not right_b64:
        return JSONResponse({"error": "Missing left or right image"}, status_code=400)

    left = decode_image(left_b64)
    right = decode_image(right_b64)
    if left is None or right is None:
        return JSONResponse({"error": "Cannot decode images"}, status_code=400)

    disp = estimator.estimate(left, right)
    h, w = disp.shape

    return {
        "disparity": encode_array(disp),
        "height": h,
        "width": w,
        "inference_time": round(estimator.inf_time, 4),
    }


@app.get("/api/status")
async def api_status():
    return {
        "model_loaded": estimator is not None,
        "model_path": model_path,
        "input_size": f"{estimator.model_w}x{estimator.model_h}" if estimator else "N/A",
        "is_combined": estimator.has_flow if estimator else False,
        "providers": estimator.session.get_providers() if estimator else [],
    }

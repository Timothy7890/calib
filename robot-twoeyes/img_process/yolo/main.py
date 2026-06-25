"""FastAPI application for YOLO11 object detection."""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from typing import List, Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from img_process.yolo.detector import YOLODetector

# --------------- Global State ---------------

detector: Optional[YOLODetector] = None
model_path: str = ""
conf_threshold: float = 0.25
iou_threshold: float = 0.45
class_names: Optional[List[str]] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global detector
    providers = None
    use_gpu = os.environ.get("YOLO_USE_GPU", "1") == "1"
    if use_gpu:
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    detector = YOLODetector(
        model_path,
        conf_threshold=conf_threshold,
        iou_threshold=iou_threshold,
        class_names=class_names,
        providers=providers,
    )
    # Warmup: run a dummy inference to trigger CUDA init + graph optimization
    dummy = np.zeros((640, 640, 3), dtype=np.uint8)
    detector.detect(dummy)
    print("[YOLO] Warmup complete, ready to serve.")
    yield
    detector = None


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------- Helpers ---------------


def decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def encode_jpeg(img: np.ndarray, quality: int = 85) -> str:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode("ascii")


# --------------- API ---------------


@app.post("/api/detect")
async def api_detect(file: UploadFile = File(...)):
    """Upload an image, return bbox-only detections."""
    data = await file.read()
    img = decode_image(data)
    if img is None:
        return JSONResponse({"error": "Cannot decode image"}, status_code=400)

    detections = detector.detect(img)
    h, w = img.shape[:2]
    return {
        "detections": detections,
        "count": len(detections),
        "image_size": [w, h],
    }


@app.post("/api/segment")
async def api_segment(file: UploadFile = File(...)):
    """Upload an image, return detections with mask contours.

    Each detection includes a "contour" field: [[x,y], [x,y], ...]
    representing the segmentation polygon of the detected object.
    """
    data = await file.read()
    img = decode_image(data)
    if img is None:
        return JSONResponse({"error": "Cannot decode image"}, status_code=400)

    detections, masks = detector.detect_with_masks(img)
    h, w = img.shape[:2]

    masks_b64 = []
    for m in masks:
        ok, buf = cv2.imencode(".png", m)
        if ok:
            masks_b64.append(base64.b64encode(buf.tobytes()).decode("ascii"))
        else:
            masks_b64.append("")

    return {
        "detections": detections,
        "masks": masks_b64,
        "count": len(detections),
        "image_size": [w, h],
    }


@app.post("/api/detect_base64")
async def api_detect_base64(payload: dict):
    """Accept base64-encoded image, return detections with masks.

    Request body: {"image": "<base64 string>"}
    """
    b64_str = payload.get("image", "")
    if not b64_str:
        return JSONResponse({"error": "Missing 'image' field"}, status_code=400)

    data = base64.b64decode(b64_str)
    img = decode_image(data)
    if img is None:
        return JSONResponse({"error": "Cannot decode image"}, status_code=400)

    detections, _ = detector.detect_with_masks(img)
    h, w = img.shape[:2]
    return {
        "detections": detections,
        "count": len(detections),
        "image_size": [w, h],
    }


@app.post("/api/detect_draw")
async def api_detect_draw(file: UploadFile = File(...)):
    """Upload an image, return detections + annotated image with masks (base64 JPEG)."""
    data = await file.read()
    img = decode_image(data)
    if img is None:
        return JSONResponse({"error": "Cannot decode image"}, status_code=400)

    vis, detections = detector.detect_and_draw(img, draw_masks=True)
    h, w = img.shape[:2]
    return {
        "detections": detections,
        "count": len(detections),
        "image_size": [w, h],
        "annotated_image": encode_jpeg(vis),
    }


@app.post("/api/detect_bytes")
async def api_detect_bytes(payload: dict):
    """Accept raw BGR bytes for internal module calls.

    Request body:
        {"data": "<base64 of raw BGR bytes>", "height": int, "width": int}
    """
    b64_data = payload.get("data", "")
    height = payload.get("height", 0)
    width = payload.get("width", 0)
    if not b64_data or not height or not width:
        return JSONResponse({"error": "Missing fields"}, status_code=400)

    raw = base64.b64decode(b64_data)
    img = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
    detections, _ = detector.detect_with_masks(img)
    return {
        "detections": detections,
        "count": len(detections),
        "image_size": [width, height],
    }


@app.get("/api/status")
async def api_status():
    return {
        "model_loaded": detector is not None,
        "model_path": model_path,
        "model_type": "segment" if detector and detector.is_seg else "detect",
        "input_size": f"{detector.input_w}x{detector.input_h}" if detector else "N/A",
        "num_classes": detector.num_classes if detector else 0,
        "providers": detector.session.get_providers() if detector else [],
        "conf_threshold": conf_threshold,
        "iou_threshold": iou_threshold,
    }


@app.get("/api/classes")
async def api_classes():
    if detector is None:
        return {"classes": []}
    return {"classes": detector.class_names}

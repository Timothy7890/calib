"""FastAPI application for depth capture — WebSocket stream + REST API."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.camera import CameraManager
from depth.stereo_depth import StereoDepthProcessor
from depth.ply_export import export_pointcloud
import backend.main as calib_module

# --------------- Global State ---------------

camera: Optional[CameraManager] = None
processor: Optional[StereoDepthProcessor] = None
save_path: Path = Path("./data/depth_captures")
capture_count: int = 0

# Store latest depth for click queries
latest_depth: Optional[np.ndarray] = None
latest_index: int = -1
use_wls: bool = True
disparity_method: str = "sgbm"


def _count_existing_captures() -> int:
    if not save_path.exists():
        return 0
    return len(list(save_path.glob("left_*.jpg")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global camera, capture_count
    host = os.environ.get("CAMERA_HOST", "127.0.0.1")
    camera = CameraManager(host=host)
    # Share camera instance with calibration module
    calib_module.camera = camera
    save_path.mkdir(parents=True, exist_ok=True)
    capture_count = _count_existing_captures()
    yield
    if camera:
        camera.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount calibration sub-app at /calibrate
app.mount("/calibrate", calib_module.app)

# --------------- Helpers ---------------


def encode_jpeg(image: np.ndarray, quality: int = 80) -> str:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode("ascii")


def white_image(h: int = 480, w: int = 640) -> np.ndarray:
    return np.full((h, w, 3), 255, dtype=np.uint8)


# --------------- WebSocket Stream ---------------


@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            pair = await asyncio.to_thread(camera.grab, 2.0)
            if pair is None:
                await asyncio.sleep(0.5)
                continue

            left, right = pair
            payload = json.dumps({
                "left": encode_jpeg(left),
            })
            await ws.send_text(payload)
            await asyncio.sleep(0.04)
    except (WebSocketDisconnect, Exception):
        pass


# --------------- REST API ---------------


@app.post("/api/capture")
async def api_capture():
    """Capture stereo pair, compute depth, generate PLY."""
    global capture_count, latest_depth, latest_index

    pair = await asyncio.to_thread(camera.grab, 3.0)
    if pair is None:
        return JSONResponse({"success": False, "error": "No frame available"}, status_code=503)

    left, right = pair

    left_rect, radial_mm, z_depth_mm = processor.compute_depth(left, right, use_wls=use_wls)
    depth_viz = processor.depth_to_colormap(radial_mm)

    # Save files
    idx_str = f"{capture_count:04d}"
    cv2.imwrite(str(save_path / f"left_{idx_str}.jpg"), left_rect)
    np.save(str(save_path / f"depth_{idx_str}.npy"), radial_mm)
    cv2.imwrite(str(save_path / f"depth_viz_{idx_str}.jpg"), depth_viz)

    # Export PLY (uses Z-depth for correct XYZ coordinates)
    K = processor.get_left_intrinsics()
    ply_path = save_path / f"pointcloud_{idx_str}.ply"
    num_points = await asyncio.to_thread(
        export_pointcloud, left_rect, z_depth_mm, K, str(ply_path),
        radial_mm=radial_mm,
    )

    # Update state (store radial distance for click queries)
    latest_depth = radial_mm
    latest_index = capture_count
    capture_count += 1

    return {
        "success": True,
        "index": latest_index,
        "count": capture_count,
        "num_points": num_points,
        "depth_viz": encode_jpeg(depth_viz),
        "left_image": encode_jpeg(left_rect),
    }


@app.get("/api/depth_at")
async def api_depth_at(index: int, x: int, y: int):
    """Query depth value at pixel (x, y) for a given capture index."""
    npy_path = save_path / f"depth_{index:04d}.npy"
    if not npy_path.exists():
        return JSONResponse({"error": "Capture not found"}, status_code=404)

    depth = np.load(str(npy_path))
    h, w = depth.shape
    if x < 0 or x >= w or y < 0 or y >= h:
        return JSONResponse({"error": "Coordinates out of bounds"}, status_code=400)

    value = float(depth[y, x])
    return {"x": x, "y": y, "depth_mm": value, "index": index}


@app.get("/api/history")
async def api_history():
    """List captured images."""
    if not save_path.exists():
        return {"captures": [], "count": 0}
    files = sorted(save_path.glob("left_*.jpg"))
    captures = [f.name.replace("left_", "").replace(".jpg", "") for f in files]
    return {"captures": captures, "count": len(captures)}


@app.get("/api/images/{filename}")
async def api_get_image(filename: str):
    """Serve a saved image file."""
    file_path = save_path / filename
    if not file_path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(file_path, media_type="image/jpeg")


@app.post("/api/config")
async def api_config(body: dict):
    """Update runtime config (WLS toggle, disparity method)."""
    global use_wls, disparity_method
    if "use_wls" in body:
        use_wls = bool(body["use_wls"])
    if "disparity_method" in body:
        method = body["disparity_method"]
        if method in ("sgbm", "crestereo"):
            disparity_method = method
            if processor is not None:
                processor.method = method
                if method == "crestereo":
                    processor._init_crestereo()
    return {"use_wls": use_wls, "disparity_method": disparity_method}


@app.get("/api/status")
async def api_status():
    from depth.stereo_depth import HAS_XIMGPROC
    return {
        "count": capture_count,
        "save_path": str(save_path),
        "image_size": f"{processor.image_width}x{processor.image_height}" if processor else "unknown",
        "use_wls": use_wls,
        "has_ximgproc": HAS_XIMGPROC,
        "disparity_method": disparity_method,
    }

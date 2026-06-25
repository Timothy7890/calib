"""FastAPI application — WebSocket stream + REST API for stereo calibration capture."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Tuple

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .camera import CameraManager
from .detection import detect_corners, parse_board_size

# --------------- Global State ---------------

camera: Optional[CameraManager] = None
save_path: Path = Path("./calib_images")
board_size: Tuple[int, int] = (9, 6)
capture_count: int = 0


def _count_existing_captures() -> int:
    """Count already-saved image pairs on disk."""
    left_dir = save_path / "left"
    if not left_dir.exists():
        return 0
    return len(list(left_dir.glob("*.jpg")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global camera, capture_count
    # When mounted as sub-app, camera is injected by the parent; only create if standalone
    if camera is None:
        host = os.environ.get("CAMERA_HOST", "127.0.0.1")
        camera = CameraManager(host=host)
    (save_path / "left").mkdir(parents=True, exist_ok=True)
    (save_path / "right").mkdir(parents=True, exist_ok=True)
    capture_count = _count_existing_captures()
    yield
    # Don't close camera when mounted — parent manages the lifecycle


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------- Helpers ---------------


def board_size_global() -> tuple[int, int]:
    return board_size


def encode_jpeg(image: np.ndarray, quality: int = 80) -> str:
    """Encode image as base64 JPEG string."""
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode("ascii")


# --------------- WebSocket Stream ---------------


@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    show_corners = True
    local_board_size = board_size

    async def receive_commands():
        nonlocal show_corners, local_board_size
        try:
            while True:
                msg = await ws.receive_text()
                data = json.loads(msg)
                if "show_corners" in data:
                    show_corners = data["show_corners"]
                if "board_size" in data:
                    local_board_size = parse_board_size(data["board_size"])
        except (WebSocketDisconnect, Exception):
            pass

    recv_task = asyncio.create_task(receive_commands())

    try:
        while True:
            pair = await asyncio.to_thread(camera.grab, 2.0)
            if pair is None:
                await asyncio.sleep(0.5)
                continue

            left, right = pair
            left_detected, left_display = detect_corners(left, local_board_size, draw=show_corners)
            right_detected, right_display = detect_corners(right, local_board_size, draw=show_corners)

            payload = json.dumps({
                "left": encode_jpeg(left_display),
                "right": encode_jpeg(right_display),
                "left_detected": left_detected,
                "right_detected": right_detected,
                "count": capture_count,
            })
            await ws.send_text(payload)
            await asyncio.sleep(0.02)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        recv_task.cancel()


# --------------- REST API ---------------


@app.post("/api/capture")
async def api_capture():
    """Capture current frame and save to disk."""
    global capture_count
    pair = await asyncio.to_thread(camera.grab, 3.0)
    if pair is None:
        return JSONResponse({"success": False, "error": "No frame available"}, status_code=503)

    left, right = pair
    idx_str = f"{capture_count:04d}"
    (save_path / "left").mkdir(parents=True, exist_ok=True)
    (save_path / "right").mkdir(parents=True, exist_ok=True)
    ok_l = cv2.imwrite(str(save_path / "left" / f"{idx_str}.jpg"), left)
    ok_r = cv2.imwrite(str(save_path / "right" / f"{idx_str}.jpg"), right)
    if not ok_l or not ok_r:
        return JSONResponse({"success": False, "error": "Failed to write image files"}, status_code=500)
    capture_count += 1
    return {"success": True, "index": capture_count - 1, "count": capture_count}


@app.get("/api/history")
async def api_history():
    """List all captured image pairs."""
    left_dir = save_path / "left"
    if not left_dir.exists():
        return {"images": []}
    files = sorted(f.name for f in left_dir.glob("*.jpg"))
    return {"images": files, "count": len(files)}


@app.get("/api/images/{side}/{filename}")
async def api_get_image(side: str, filename: str, corners: int = 0, board_size: str = ""):
    """Serve a saved image file, optionally with corners drawn."""
    if side not in ("left", "right"):
        return JSONResponse({"error": "side must be 'left' or 'right'"}, status_code=400)
    file_path = save_path / side / filename
    if not file_path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)

    if not corners:
        return FileResponse(file_path, media_type="image/jpeg")

    bs = parse_board_size(board_size) if board_size else board_size_global()
    img = cv2.imread(str(file_path))
    if img is None:
        return JSONResponse({"error": "Failed to read image"}, status_code=500)
    _, drawn = detect_corners(img, bs, draw=True)
    ok, buf = cv2.imencode(".jpg", drawn, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return JSONResponse({"error": "Encode failed"}, status_code=500)
    from fastapi.responses import Response
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/api/status")
async def api_status():
    """Current application status."""
    return {
        "count": capture_count,
        "board_size": f"{board_size[0]}x{board_size[1]}",
        "save_path": str(save_path),
    }


@app.post("/api/config")
async def api_config(body: dict):
    """Update board size configuration."""
    global board_size
    if "board_size" in body:
        board_size = parse_board_size(body["board_size"])
    return {"board_size": f"{board_size[0]}x{board_size[1]}"}

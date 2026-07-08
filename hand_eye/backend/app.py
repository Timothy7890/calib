"""FastAPI app: left-eye stream + click-to-capture (image + joint angles).

This is the data-collection half of hand-eye calibration. Each capture stores a
synchronized pair:
    <save_path>/left/NNNN.jpg     left-eye image
    <save_path>/joints/NNNN.json  right-arm joint vector at capture time

State (camera, joint_provider, save_path, board_size) is injected by run_server.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .camera import HeadCamera
from .detection import detect_corners, parse_board_size
from .joints import JointProvider, MockJointProvider

# --------------- Injected global state ---------------

camera: Optional[HeadCamera] = None
joint_provider: JointProvider = MockJointProvider()
save_path: Path = Path("./handeye_data")
board_size: Tuple[int, int] = (11, 8)
capture_count: int = 0
# Optional right-arm jog controller (set by run_server when --arm-control is on).
arm_controller = None


def _count_existing_captures() -> int:
    left_dir = save_path / "left"
    if not left_dir.exists():
        return 0
    return len(list(left_dir.glob("*.jpg")))


def init_state() -> None:
    """Create directories and sync the capture counter. Call after injection."""
    global capture_count
    (save_path / "left").mkdir(parents=True, exist_ok=True)
    (save_path / "right").mkdir(parents=True, exist_ok=True)
    (save_path / "joints").mkdir(parents=True, exist_ok=True)
    (save_path / "tcp").mkdir(parents=True, exist_ok=True)
    capture_count = _count_existing_captures()
    _write_session_meta()


def _next_tcp_index() -> int:
    """Next free TCP record index (robust to deletions of earlier records)."""
    tcp_dir = save_path / "tcp"
    used = [int(p.stem) for p in tcp_dir.glob("*.json") if p.stem.isdigit()]
    return (max(used) + 1) if used else 0


def _write_session_meta() -> None:
    meta = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "board_size": f"{board_size[0]}x{board_size[1]}",
        "joint_source": joint_provider.source,
        "joint_names": list(joint_provider.joint_names),
        "note": "left/NNNN.jpg pairs with joints/NNNN.json captured at the same instant.",
    }
    try:
        with open(save_path / "session_meta.json", "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


app = FastAPI(title="Hand-Eye Capture")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def encode_jpeg(image: np.ndarray, quality: int = 80) -> str:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode("ascii")


# --------------- WebSocket: left-eye stream ---------------


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
                    show_corners = bool(data["show_corners"])
                if "board_size" in data:
                    local_board_size = parse_board_size(data["board_size"])
        except (WebSocketDisconnect, Exception):
            pass

    recv_task = asyncio.create_task(receive_commands())
    try:
        while True:
            pair = await asyncio.to_thread(camera.grab_pair, 2.0)
            if pair is None:
                await asyncio.sleep(0.3)
                continue
            left, right = pair
            left_detected, left_disp = detect_corners(left, local_board_size, draw=show_corners)
            right_detected, right_disp = detect_corners(right, local_board_size, draw=show_corners)
            payload = json.dumps(
                {
                    "left": encode_jpeg(left_disp),
                    "right": encode_jpeg(right_disp),
                    "left_detected": left_detected,
                    "right_detected": right_detected,
                    "count": capture_count,
                }
            )
            await ws.send_text(payload)
            await asyncio.sleep(0.04)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        recv_task.cancel()


# --------------- REST API ---------------


@app.get("/api/joints")
async def api_joints():
    """Current joint vector (polled by the UI to show the live pose)."""
    try:
        q = await asyncio.to_thread(joint_provider.read)
        return {
            "ok": True,
            "source": joint_provider.source,
            "joint_names": list(joint_provider.joint_names),
            "q": np.asarray(q, dtype=float).reshape(-1).tolist(),
        }
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "source": joint_provider.source, "error": str(exc)},
            status_code=503,
        )


@app.post("/api/capture")
async def api_capture():
    """Capture left image + joints atomically under the same index."""
    global capture_count

    pair = await asyncio.to_thread(camera.grab_pair, 3.0)
    if pair is None:
        return JSONResponse({"success": False, "error": "No camera frame available"}, status_code=503)
    left, right = pair

    try:
        q = await asyncio.to_thread(joint_provider.read)
    except Exception as exc:
        return JSONResponse(
            {"success": False, "error": f"Failed to read joints ({joint_provider.source}): {exc}"},
            status_code=503,
        )

    q_arr = np.asarray(q, dtype=float).reshape(-1)
    if not np.all(np.isfinite(q_arr)):
        return JSONResponse({"success": False, "error": f"Non-finite joints: {q_arr}"}, status_code=500)

    idx_str = f"{capture_count:04d}"
    left_file = save_path / "left" / f"{idx_str}.jpg"
    right_file = save_path / "right" / f"{idx_str}.jpg"
    joints_file = save_path / "joints" / f"{idx_str}.json"

    if not cv2.imwrite(str(left_file), left):
        return JSONResponse({"success": False, "error": "Failed to write left image file"}, status_code=500)
    if not cv2.imwrite(str(right_file), right):
        left_file.unlink(missing_ok=True)
        return JSONResponse({"success": False, "error": "Failed to write right image file"}, status_code=500)

    record = {
        "index": capture_count,
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "joint_source": joint_provider.source,
        "joint_names": list(joint_provider.joint_names),
        "q_rad": q_arr.tolist(),
        "image": f"left/{idx_str}.jpg",
        "image_right": f"right/{idx_str}.jpg",
    }
    try:
        with open(joints_file, "w") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        left_file.unlink(missing_ok=True)
        right_file.unlink(missing_ok=True)
        return JSONResponse({"success": False, "error": f"Failed to write joints: {exc}"}, status_code=500)

    capture_count += 1
    return {
        "success": True,
        "index": capture_count - 1,
        "count": capture_count,
        "q_rad": q_arr.tolist(),
        "joint_names": list(joint_provider.joint_names),
    }


@app.get("/api/history")
async def api_history():
    left_dir = save_path / "left"
    if not left_dir.exists():
        return {"images": [], "count": 0}
    files = sorted(f.name for f in left_dir.glob("*.jpg"))
    return {"images": files, "count": len(files)}


@app.get("/api/images/left/{filename}")
async def api_get_image(filename: str):
    file_path = save_path / "left" / filename
    if not file_path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(file_path, media_type="image/jpeg")


@app.get("/api/images/right/{filename}")
async def api_get_image_right(filename: str):
    file_path = save_path / "right" / filename
    if not file_path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(file_path, media_type="image/jpeg")


@app.get("/api/status")
async def api_status():
    return {
        "count": capture_count,
        "board_size": f"{board_size[0]}x{board_size[1]}",
        "save_path": str(save_path),
        "joint_source": joint_provider.source,
        "joint_names": list(joint_provider.joint_names),
        "camera_source": getattr(camera, "source", "unknown"),
    }


@app.post("/api/config")
async def api_config(body: dict):
    global board_size
    if "board_size" in body:
        board_size = parse_board_size(body["board_size"])
        _write_session_meta()
    return {"board_size": f"{board_size[0]}x{board_size[1]}"}


# --------------- TCP (tool-center-point) pivot capture ---------------
#
# For TCP calibration we record ONLY joint angles (no image, no board). The user
# touches one fixed reference point in space with the tool tip from many arm
# poses; solve_tcp.py then runs the pivot least-squares. Records carry a "group"
# label so the two-point axis method can separate, e.g. the needle TIP from a
# second point along the needle ("p2").


@app.post("/api/tcp/capture")
async def api_tcp_capture(body: dict):
    """Record current joints for a TCP pivot pose. Body: {"group": "tip"|"p2"|...}."""
    group = str(body.get("group", "tip")).strip() or "tip"

    try:
        q = await asyncio.to_thread(joint_provider.read)
    except Exception as exc:
        return JSONResponse(
            {"success": False, "error": f"Failed to read joints ({joint_provider.source}): {exc}"},
            status_code=503,
        )

    q_arr = np.asarray(q, dtype=float).reshape(-1)
    if not np.all(np.isfinite(q_arr)):
        return JSONResponse({"success": False, "error": f"Non-finite joints: {q_arr}"}, status_code=500)

    index = _next_tcp_index()
    rec_file = save_path / "tcp" / f"{index:04d}.json"
    record = {
        "index": index,
        "group": group,
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "joint_source": joint_provider.source,
        "joint_names": list(joint_provider.joint_names),
        "q_rad": q_arr.tolist(),
    }
    try:
        with open(rec_file, "w") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return JSONResponse({"success": False, "error": f"Failed to write tcp record: {exc}"}, status_code=500)

    return {
        "success": True,
        "index": index,
        "group": group,
        "q_rad": q_arr.tolist(),
        "joint_names": list(joint_provider.joint_names),
    }


@app.get("/api/tcp/list")
async def api_tcp_list():
    tcp_dir = save_path / "tcp"
    items = []
    if tcp_dir.exists():
        for f in sorted(tcp_dir.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            items.append(
                {
                    "index": rec.get("index"),
                    "group": rec.get("group", "?"),
                    "datetime": rec.get("datetime", ""),
                }
            )
    groups: dict = {}
    for it in items:
        groups[it["group"]] = groups.get(it["group"], 0) + 1
    return {"items": items, "count": len(items), "groups": groups}


@app.delete("/api/tcp/{index}")
async def api_tcp_delete(index: int):
    f = save_path / "tcp" / f"{index:04d}.json"
    if not f.exists():
        return JSONResponse({"success": False, "error": "not found"}, status_code=404)
    try:
        f.unlink()
    except OSError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)
    return {"success": True}


# --------------- Right-arm jog control (only if arm_controller is set) ---------------


@app.get("/api/arm/status")
async def api_arm_status():
    if arm_controller is None:
        return JSONResponse({"available": False}, status_code=200)
    st = await asyncio.to_thread(arm_controller.status)
    st["available"] = True
    return st


@app.post("/api/arm/enable")
async def api_arm_enable():
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    await asyncio.to_thread(arm_controller.enable_jog)
    return {"success": True}


@app.post("/api/arm/disable")
async def api_arm_disable():
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    await asyncio.to_thread(arm_controller.disable_jog)
    return {"success": True}


@app.post("/api/arm/stop")
async def api_arm_stop():
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    await asyncio.to_thread(arm_controller.stop)
    return {"success": True}


@app.post("/api/arm/handmove")
async def api_arm_handmove():
    """Enter compliant hand-guide mode (right arm goes soft). Operator MUST support it."""
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    ok = await asyncio.to_thread(arm_controller.enter_hand_move)
    if not ok:
        return JSONResponse(
            {"success": False, "error": "disable jog first (jog is enabled)"},
            status_code=409,
        )
    return {"success": True}


@app.post("/api/arm/set")
async def api_arm_set(body: dict):
    """Set absolute joint targets. Body: {"q": [rad, ... x7]}."""
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    q = body.get("q")
    if q is None:
        return JSONResponse({"success": False, "error": "missing 'q'"}, status_code=400)
    try:
        ok = await asyncio.to_thread(arm_controller.set_target, q)
    except (ValueError, IndexError) as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
    if not ok:
        return JSONResponse({"success": False, "error": "jog not enabled"}, status_code=409)
    return {"success": True}


@app.post("/api/arm/nudge")
async def api_arm_nudge(body: dict):
    """Nudge one joint. Body: {"index": i, "delta": rad}."""
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    try:
        index = int(body["index"])
        delta = float(body["delta"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"success": False, "error": "need integer 'index' and float 'delta'"}, status_code=400)
    try:
        ok = await asyncio.to_thread(arm_controller.nudge, index, delta)
    except IndexError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
    if not ok:
        return JSONResponse({"success": False, "error": "jog not enabled"}, status_code=409)
    return {"success": True}

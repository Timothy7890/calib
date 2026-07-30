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
import re
import subprocess
import sys
import threading
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
# Solver defaults (injected by run_server).
urdf_path: str = ""
base_link: str = "torso_link"
tip_link: str = "right_dex1_tool_link"


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


@app.post("/api/arm/release")
async def api_arm_release():
    """放弃接管: stop low-level streaming and give control back to the body
    controller. The operator should support the arm before calling."""
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    restored = await asyncio.to_thread(arm_controller.release_takeover)
    return {"success": True, "restored_mode": restored}


@app.post("/api/arm/engage")
async def api_arm_engage():
    """重新接管: release the motion mode again and hold at the current pose."""
    if arm_controller is None:
        return JSONResponse({"success": False, "error": "arm control disabled"}, status_code=409)
    await asyncio.to_thread(arm_controller.reengage)
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


# --------------- One-click hand-eye solve (runs solve_handeye.py) ---------------

_HANDEYE_ROOT = Path(__file__).resolve().parents[1]
_CALIB_ROOT = _HANDEYE_ROOT.parent
_TS_DIR_RE = re.compile(r"^\d{8}_\d{6}$")

solve_job: dict = {"running": False, "session": "", "log": [], "result": None, "error": ""}
_solve_lock = threading.Lock()


def _sessions_base() -> Path:
    """Base dir that holds capture session folders.

    run_server normally saves into <base>/<YYYYmmdd_HHMMSS>; with
    --no-timestamp-dir, save_path itself is the base.
    """
    if _TS_DIR_RE.match(save_path.name):
        return save_path.parent
    return save_path


def _session_resolution(data_dir: Path) -> Optional[Tuple[int, int]]:
    """(width, height) of the first captured left image, or None."""
    left_dir = data_dir / "left"
    if not left_dir.is_dir():
        return None
    for f in sorted(left_dir.glob("*.jpg")):
        img = cv2.imread(str(f))
        if img is not None:
            h, w = img.shape[:2]
            return (w, h)
    return None


def _intrinsics_resolution(yaml_path: Path) -> Optional[Tuple[int, int]]:
    """(width, height) recorded in a stereo_calibration.yaml, or None."""
    try:
        fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_READ)
        if not fs.isOpened():
            return None
        w = fs.getNode("image_width").real()
        h = fs.getNode("image_height").real()
        fs.release()
        if w and h and w > 0 and h > 0:
            return (int(w), int(h))
    except cv2.error:
        pass
    return None


@app.get("/api/solve/sessions")
async def api_solve_sessions():
    base = _sessions_base()
    sessions = []
    if base.exists():
        for d in sorted(base.iterdir(), reverse=True):
            if not d.is_dir() or not (d / "joints").is_dir():
                continue
            n_images = len(list((d / "left").glob("*.jpg"))) if (d / "left").is_dir() else 0
            n_joints = len(list((d / "joints").glob("*.json")))
            has_result = (d / "handeye_result.json").exists() or (
                d / "handeye_result_left.json").exists()
            board = ""
            meta_f = d / "session_meta.json"
            if meta_f.exists():
                try:
                    board = json.loads(meta_f.read_text()).get("board_size", "")
                except (OSError, json.JSONDecodeError):
                    pass
            res = _session_resolution(d)
            sessions.append({
                "name": d.name,
                "n_images": n_images,
                "n_joints": n_joints,
                "board_size": board,
                "resolution": f"{res[0]}x{res[1]}" if res else "",
                "has_result": has_result,
                "is_current": d.resolve() == save_path.resolve(),
            })
    return {"base": str(base), "sessions": sessions}


@app.get("/api/solve/intrinsics")
async def api_solve_intrinsics():
    """List stereo_calibration.yaml files produced by the robot-twoeyes tool."""
    items = []
    calib_images = _CALIB_ROOT / "robot-twoeyes" / "data" / "calib_images"
    if calib_images.exists():
        for d in sorted(calib_images.iterdir(), reverse=True):
            f = d / "stereo_calibration.yaml"
            if d.is_dir() and f.exists():
                res = _intrinsics_resolution(f)
                items.append({
                    "label": d.name,
                    "path": str(f),
                    "resolution": f"{res[0]}x{res[1]}" if res else "",
                })
    return {"items": items}


def _load_solve_result(data_dir: Path) -> Optional[dict]:
    combined = data_dir / "handeye_result.json"
    if combined.exists():
        try:
            return json.loads(combined.read_text())
        except (OSError, json.JSONDecodeError):
            return None
    out: dict = {}
    for eye in ("left", "right"):
        f = data_dir / f"handeye_result_{eye}.json"
        if f.exists():
            try:
                out[eye] = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                pass
    return out or None


def _run_solve_job(cmd: list, data_dir: Path) -> None:
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_HANDEYE_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            with _solve_lock:
                solve_job["log"].append(line.rstrip("\n"))
                if len(solve_job["log"]) > 800:
                    del solve_job["log"][:200]
        rc = proc.wait()
        result = _load_solve_result(data_dir)
        with _solve_lock:
            if rc != 0 and result is None:
                solve_job["error"] = f"solve_handeye.py exited with code {rc}"
            solve_job["result"] = result
    except Exception as exc:
        with _solve_lock:
            solve_job["error"] = str(exc)
    finally:
        with _solve_lock:
            solve_job["running"] = False


@app.post("/api/solve")
async def api_solve(body: dict):
    """Start a background hand-eye solve. Body:
    {"session": str, "intrinsics": str, "square_size_mm": float,
     "board_size": str?, "method": str?, "eye": str?}
    """
    with _solve_lock:
        if solve_job["running"]:
            return JSONResponse(
                {"success": False, "error": f"已有求解在运行: {solve_job['session']}"},
                status_code=409,
            )

    session = str(body.get("session", "")).strip()
    data_dir = (_sessions_base() / session).resolve()
    if not session or _sessions_base().resolve() not in data_dir.parents:
        return JSONResponse({"success": False, "error": "无效的 session"}, status_code=400)
    if not (data_dir / "joints").is_dir():
        return JSONResponse({"success": False, "error": f"{data_dir} 缺少 joints/"}, status_code=400)

    intrinsics = str(body.get("intrinsics", "")).strip()
    if not intrinsics or not Path(intrinsics).exists():
        return JSONResponse({"success": False, "error": f"内参文件不存在: {intrinsics}"}, status_code=400)

    # 防呆:内参标定分辨率必须与该会话图像分辨率一致(内参不能跨分辨率使用)。
    sess_res = _session_resolution(data_dir)
    intr_res = _intrinsics_resolution(Path(intrinsics))
    if sess_res is None:
        return JSONResponse({"success": False, "error": f"{data_dir} 的 left/ 里没有可读图像"}, status_code=400)
    if intr_res is not None and sess_res != intr_res:
        return JSONResponse(
            {
                "success": False,
                "error": (
                    f"分辨率不匹配:会话图像为 {sess_res[0]}x{sess_res[1]},"
                    f"但所选内参标定于 {intr_res[0]}x{intr_res[1]}。"
                    f"请选择相同分辨率的 stereo_calibration.yaml。"
                ),
            },
            status_code=400,
        )

    try:
        square = float(body.get("square_size_mm"))
        if not (0.1 < square < 1000):
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({"success": False, "error": "square_size_mm 无效"}, status_code=400)

    bs = str(body.get("board_size") or f"{board_size[0]}x{board_size[1]}")
    method = str(body.get("method") or "park")
    if method not in ("tsai", "park", "horaud", "andreff", "daniilidis"):
        return JSONResponse({"success": False, "error": f"未知 method: {method}"}, status_code=400)
    eye = str(body.get("eye") or "both")
    if eye not in ("left", "right", "both"):
        return JSONResponse({"success": False, "error": f"未知 eye: {eye}"}, status_code=400)

    if not urdf_path or not Path(urdf_path).exists():
        return JSONResponse(
            {"success": False, "error": f"服务器 URDF 不存在: {urdf_path}(用 --urdf 指定)"},
            status_code=500,
        )

    cmd = [
        sys.executable, "-u", str(_HANDEYE_ROOT / "solve_handeye.py"),
        "--data", str(data_dir),
        "--intrinsics", intrinsics,
        "--urdf", urdf_path,
        "--board-size", bs,
        "--square-size", str(square),
        "--base-link", base_link,
        "--tip-link", tip_link,
        "--method", method,
        "--eye", eye,
    ]

    with _solve_lock:
        solve_job.update({
            "running": True,
            "session": session,
            "log": ["$ " + " ".join(cmd)],
            "result": None,
            "error": "",
        })
    threading.Thread(target=_run_solve_job, args=(cmd, data_dir), daemon=True).start()
    return {"success": True, "session": session}


@app.get("/api/solve/status")
async def api_solve_status():
    with _solve_lock:
        return {
            "running": solve_job["running"],
            "session": solve_job["session"],
            "log": list(solve_job["log"]),
            "result": solve_job["result"],
            "error": solve_job["error"],
            "urdf": urdf_path,
            "base_link": base_link,
            "tip_link": tip_link,
        }


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

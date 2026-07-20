"""FastAPI app：彩色流预览 + 点击取 P_camera + 手腕位姿配对 + 联合解算。

每个样本 = P_camera（点击反投影）+ T_base^wrist（自动读取或手填 xyz+rpy）。
解算联合估计 T_base^camera 和指尖偏移 p_tool（腕系），不需要事先量偏移。
样本落盘为 <save_path>/samples/NNNN.json，重启不丢。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .camera import CameraBase, MockCamera
from .robot import ManualPoseProvider, PoseProvider
from .solver import (
    MIN_SAMPLES_TOOL,
    leave_one_out_tool,
    make_T,
    rpy_to_rot,
    solve_with_tool_offset,
)

# --------------- 注入的全局状态 ---------------

camera: CameraBase = MockCamera()
pose_provider: PoseProvider = ManualPoseProvider()
save_path: Path = Path("./handeye3d_data")

app = FastAPI(title="Hand-Eye 3D (point + wrist-pose) Calibration")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def init_state() -> None:
    (save_path / "samples").mkdir(parents=True, exist_ok=True)


def _samples_dir() -> Path:
    return save_path / "samples"


def _load_samples() -> list[dict]:
    items = []
    for f in sorted(_samples_dir().glob("*.json")):
        try:
            items.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return items


def _next_index() -> int:
    used = [int(p.stem) for p in _samples_dir().glob("*.json") if p.stem.isdigit()]
    return (max(used) + 1) if used else 0


# --------------- 状态 / 相机 ---------------


@app.get("/api/status")
async def api_status():
    return {
        "camera": camera.info(),
        "pose_source": pose_provider.source,
        "pose_auto": pose_provider.available,
        "base_link": pose_provider.base_link,
        "wrist_link": pose_provider.wrist_link,
        "save_path": str(save_path),
        "sample_count": len(_load_samples()),
        "min_samples": MIN_SAMPLES_TOOL,
    }


@app.get("/api/stream")
async def api_stream():
    """彩色相机 MJPEG 预览流。"""

    def gen():
        while True:
            data = camera.get_jpeg()
            if data is None:
                time.sleep(0.2)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                   + data + b"\r\n")
            time.sleep(0.05)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/pick")
async def api_pick(body: dict):
    """点击像素反投影。Body: {"u": int, "v": int}，返回彩色相机系坐标（米）。"""
    try:
        u, v = int(body["u"]), int(body["v"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "需要整数 u、v"}, status_code=400)
    result = await asyncio.to_thread(camera.pick, u, v)
    status = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status)


@app.get("/api/wrist_pose")
async def api_wrist_pose():
    """自动读取当前手腕位姿（pose_provider 可用时）。"""
    if not pose_provider.available:
        return JSONResponse(
            {"ok": False, "error": f"pose source '{pose_provider.source}' 不支持自动读取，请手填"},
            status_code=409,
        )
    try:
        T = await asyncio.to_thread(pose_provider.read_pose)
        return {"ok": True, "T_base_wrist": np.asarray(T, dtype=float).reshape(4, 4).tolist(),
                "source": pose_provider.source}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


# --------------- 样本管理 ---------------


def _parse_wrist_pose(body: dict) -> np.ndarray:
    """接受 {"T_base_wrist": 4x4} 或 {"wrist_xyz": [3], "wrist_rpy": [3]}（弧度）。"""
    if "T_base_wrist" in body:
        T = np.asarray(body["T_base_wrist"], dtype=float).reshape(4, 4)
    elif "wrist_xyz" in body and "wrist_rpy" in body:
        xyz = [float(v) for v in body["wrist_xyz"]]
        rpy = [float(v) for v in body["wrist_rpy"]]
        T = make_T(rpy_to_rot(*rpy), xyz)
    else:
        raise ValueError("需要 T_base_wrist（4x4）或 wrist_xyz + wrist_rpy")
    if not np.all(np.isfinite(T)):
        raise ValueError("手腕位姿包含非法值")
    return T


@app.get("/api/samples")
async def api_samples():
    items = _load_samples()
    return {"samples": items, "count": len(items)}


@app.post("/api/samples")
async def api_add_sample(body: dict):
    """保存一个样本。Body: {"p_camera": [3], "T_base_wrist": 4x4 或 wrist_xyz+wrist_rpy, "pixel": [u,v]?}"""
    try:
        p_cam = np.asarray(body["p_camera"], dtype=float).reshape(3)
        T_wrist = _parse_wrist_pose(body)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not np.all(np.isfinite(p_cam)):
        return JSONResponse({"ok": False, "error": "p_camera 包含非法值"}, status_code=400)

    index = _next_index()
    record = {
        "index": index,
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "p_camera": p_cam.tolist(),
        "T_base_wrist": T_wrist.tolist(),
        "pixel": body.get("pixel"),
        "pose_source": pose_provider.source,
        "camera": {k: camera.info().get(k) for k in ("serial", "source")},
    }
    (_samples_dir() / f"{index:04d}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False))
    return {"ok": True, "index": index, "count": len(_load_samples())}


@app.delete("/api/samples/{index}")
async def api_delete_sample(index: int):
    f = _samples_dir() / f"{index:04d}.json"
    if not f.exists():
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    f.unlink()
    return {"ok": True, "count": len(_load_samples())}


# --------------- 解算 ---------------


@app.post("/api/solve")
async def api_solve():
    samples = _load_samples()
    if len(samples) < MIN_SAMPLES_TOOL:
        return JSONResponse(
            {"ok": False, "error": f"联合解至少 {MIN_SAMPLES_TOOL} 个样本，当前 {len(samples)} 个"},
            status_code=400)
    p_cam = np.array([s["p_camera"] for s in samples])
    T_wrist = np.array([s["T_base_wrist"] for s in samples])
    try:
        result = await asyncio.to_thread(solve_with_tool_offset, p_cam, T_wrist)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    loo = await asyncio.to_thread(leave_one_out_tool, p_cam, T_wrist)
    result["leave_one_out_mm"] = loo
    finite = [e for e in loo if np.isfinite(e)]
    if finite:
        result["leave_one_out_stats_mm"] = {
            "mean": float(np.mean(finite)), "max": float(np.max(finite)),
        }
    result["ok"] = True
    result["sample_indices"] = [s["index"] for s in samples]
    result["solved_at"] = datetime.now().isoformat(timespec="seconds")
    result["base_link"] = pose_provider.base_link
    result["wrist_link"] = pose_provider.wrist_link
    result["camera"] = camera.info()

    out = save_path / "handeye3d_result.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    result["saved_to"] = str(out)
    return result

"""FastAPI app：彩色流预览 + 点击取 P_camera + 手腕位姿配对 + 联合解算。

每个样本 = P_camera（点击反投影）+ T_base^wrist（自动读取或手填 xyz+rpy）。
解算联合估计 T_base^camera 和指尖偏移 p_tool（腕系），不需要事先量偏移。
样本落盘为 <save_path>/samples/NNNN.json，重启不丢。
"""

from __future__ import annotations

import asyncio
import json
import threading
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
    MIN_SAMPLES_PIVOT,
    MIN_SAMPLES_TOOL,
    MIN_SAMPLES_TOOL_ONLY,
    leave_one_out_pivot,
    leave_one_out_tool,
    make_T,
    rpy_to_rot,
    solve_pivot,
    solve_tool_fixed_cam,
    solve_with_tool_offset,
)

# --------------- 注入的全局状态 ---------------

camera: CameraBase = MockCamera()
pose_provider: PoseProvider = ManualPoseProvider()
arm_factory = None      # run_server 传 --arm-control 时注入（工厂，点「获取控制」才创建）
arm_controller = None   # 当前接管中的 H2ArmController（None = 未接管）
arm_lock = threading.Lock()
save_path: Path = Path("./handeye3d_data")

app = FastAPI(title="Hand-Eye 3D (point + wrist-pose) Calibration")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def init_state() -> None:
    (save_path / "samples").mkdir(parents=True, exist_ok=True)
    (save_path / "pivot_samples").mkdir(parents=True, exist_ok=True)


def _samples_dir() -> Path:
    return save_path / "samples"


def _pivot_dir() -> Path:
    return save_path / "pivot_samples"


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


# --------------- 手臂点动（可选，--arm-control 时启用） ---------------


def _arm_absent():
    if arm_factory is None:
        return JSONResponse(
            {"ok": False, "error": "未启用手臂控制，启动时加 --arm-control"}, status_code=409)
    return JSONResponse(
        {"ok": False, "error": "尚未接管手臂，请先点「获取控制」"}, status_code=409)


@app.get("/api/arm/status")
async def api_arm_status():
    if arm_factory is None and arm_controller is None:
        return {"enabled": False}
    if arm_controller is None:
        return {"enabled": True, "armed": False}
    st = arm_controller.status()
    st["enabled"] = True
    st["armed"] = True
    return st


@app.post("/api/arm/engage")
def api_arm_engage():
    """获取控制：创建控制器、发布 rt/arm_sdk、在当前姿态刚性保持。真机会被接管！

    同步 def：跑在线程池里，创建控制器（DDS 握手，可能几秒）不会卡住事件循环。
    """
    global arm_controller
    if arm_factory is None:
        return _arm_absent()
    with arm_lock:
        if arm_controller is not None:
            return {"ok": True, "armed": True, "message": "已处于接管状态"}
        try:
            controller = arm_factory()
            controller.start()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"接管失败: {exc}"}, status_code=502)
        arm_controller = controller
    print("[handeye3d] 已接管手臂，开始发布 rt/arm_sdk")
    print(f"[handeye3d] 重力前馈: {controller.describe_gravity()}")
    return {"ok": True, "armed": True, **controller.status()}


@app.post("/api/arm/disarm")
def api_arm_disarm():
    """归还控制：权重渐出、交还本体控制器。调用前请扶住手臂。"""
    global arm_controller
    with arm_lock:
        if arm_controller is None:
            return {"ok": True, "armed": False, "message": "本来就未接管"}
        controller, arm_controller = arm_controller, None
    controller.shutdown()
    print("[handeye3d] 已归还手臂控制权")
    return {"ok": True, "armed": False, "message": "已归还，控制权交还本体控制器"}


@app.post("/api/arm/enable_jog")
async def api_arm_enable_jog():
    if arm_controller is None:
        return _arm_absent()
    arm_controller.enable_jog()
    return {"ok": True, **arm_controller.status()}


@app.post("/api/arm/disable_jog")
async def api_arm_disable_jog():
    if arm_controller is None:
        return _arm_absent()
    arm_controller.disable_jog()
    return {"ok": True, **arm_controller.status()}


@app.post("/api/arm/stop")
async def api_arm_stop():
    """冻结在当前指令位并刚性保持（也用于退出卸力）。"""
    if arm_controller is None:
        return _arm_absent()
    arm_controller.stop()
    return {"ok": True, **arm_controller.status()}


@app.post("/api/arm/hand_move")
async def api_arm_hand_move():
    """卸力拖动模式：kp=0 只留阻尼，手臂会下坠，必须有人扶住！"""
    if arm_controller is None:
        return _arm_absent()
    ok = arm_controller.enter_hand_move()
    if not ok:
        return JSONResponse(
            {"ok": False, "error": "点动开启时不能进入卸力模式，请先停止点动"}, status_code=409)
    return {"ok": True, **arm_controller.status()}


@app.post("/api/arm/nudge")
async def api_arm_nudge(body: dict):
    """单关节步进。Body: {"index": int, "delta": float}（弧度）。"""
    if arm_controller is None:
        return _arm_absent()
    try:
        index = int(body["index"])
        delta = float(body["delta"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "需要 index(int) 和 delta(float)"},
                            status_code=400)
    try:
        accepted = arm_controller.nudge(index, delta)
    except (IndexError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not accepted:
        return JSONResponse({"ok": False, "error": "点动未开启（或处于卸力模式）"},
                            status_code=409)
    return {"ok": True, **arm_controller.status()}


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


DEPTH_MIN_M = 0.30   # 双目最近测距标称 0.25m，但 0.3m 内实测有系统偏差
                     # （20260726_235153 会话：<0.3m 的样本残差 9~17mm，>0.3m 的 4~7mm）
DEPTH_MAX_M = 1.5    # 标定时指尖不该离相机超过这个距离，超了就是点到背景/飞点


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
    if not (DEPTH_MIN_M <= float(p_cam[2]) <= DEPTH_MAX_M):
        return JSONResponse(
            {"ok": False, "error": f"深度 {p_cam[2]:.2f}m 超出 {DEPTH_MIN_M}~{DEPTH_MAX_M}m，"
                                   "像是点到背景（边缘飞点）或离相机太近——往手指内侧一点重新点击"},
            status_code=400)

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


def _find_latest_calib() -> Path | None:
    """找最新一份手眼标定结果：先看本会话目录，再翻数据根目录下各时间戳会话。"""
    candidates = [save_path / "handeye3d_result.json"]
    parent = save_path.parent
    if parent.is_dir():
        candidates += list(parent.glob("*/handeye3d_result.json"))
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


# --------------- 只解指尖偏移（固定相机外参，点击指尖尖端采样） ---------------


@app.post("/api/solve_tool")
async def api_solve_tool(body: dict | None = None):
    """固定 T_base^camera，只解 p_tool。样本 = 点击指尖尖端 + 腕位姿（与联合解共用）。

    Body 可选: {"calib_path": "..."}，默认自动用最新一份 handeye3d_result.json。
    """
    body = body or {}
    calib_path = Path(body["calib_path"]) if body.get("calib_path") else _find_latest_calib()
    if calib_path is None or not calib_path.is_file():
        return JSONResponse(
            {"ok": False, "error": "找不到已有的手眼标定结果（handeye3d_result.json），"
                                   "请先做一次联合解算或指定 calib_path"},
            status_code=400)
    try:
        calib = json.loads(calib_path.read_text())
        R = np.asarray(calib["R_cam2base"], dtype=float).reshape(3, 3)
        t = np.asarray(calib["t_cam2base_m"], dtype=float).reshape(3)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": f"标定文件无法解析: {exc}"}, status_code=400)

    samples = _load_samples()
    if len(samples) < MIN_SAMPLES_TOOL_ONLY:
        return JSONResponse(
            {"ok": False, "error": f"至少 {MIN_SAMPLES_TOOL_ONLY} 个样本，当前 {len(samples)} 个"},
            status_code=400)
    p_cam = np.array([s["p_camera"] for s in samples])
    T_wrist = np.array([s["T_base_wrist"] for s in samples])
    indices = [s["index"] for s in samples]

    # 自动剔除离群样本（飞点/采样时手臂在动）：反复"解算→踢掉最差的"，
    # 直到最差残差可接受或只剩下限个样本。被剔除的会如实报告。
    keep = np.arange(len(samples))
    dropped: list[dict] = []
    try:
        while True:
            result = await asyncio.to_thread(
                solve_tool_fixed_cam, p_cam[keep], T_wrist[keep], R, t)
            errs = np.asarray(result["residual_mm"]["per_sample"], dtype=float)
            worst = int(np.argmax(errs))
            median = float(np.median(errs))
            if len(keep) <= MIN_SAMPLES_TOOL_ONLY or \
                    errs[worst] <= max(30.0, 5.0 * median):
                break
            dropped.append({"index": indices[keep[worst]],
                            "residual_mm": round(float(errs[worst]), 1)})
            keep = np.delete(keep, worst)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    result["ok"] = True
    result["sample_indices"] = [indices[i] for i in keep]
    result["dropped_samples"] = dropped
    result["solved_at"] = datetime.now().isoformat(timespec="seconds")
    result["calib_used"] = str(calib_path)
    result["base_link"] = pose_provider.base_link
    result["wrist_link"] = pose_provider.wrist_link

    old = np.asarray(calib.get("p_tool_wrist_m", []), dtype=float)
    new = np.asarray(result["p_tool_wrist_m"], dtype=float)
    if old.shape == (3,):
        result["delta_vs_calib_mm"] = ((new - old) * 1000.0).tolist()
        result["delta_vs_calib_norm_mm"] = float(np.linalg.norm(new - old) * 1000.0)

    # 生成替换了 p_tool 的完整标定文件，可直接给 reach_server --calib 用
    merged = dict(calib)
    merged["p_tool_wrist_m"] = result["p_tool_wrist_m"]
    merged["p_tool_source"] = "tool_only_fixed_cam"
    merged["tool_solved_at"] = result["solved_at"]
    merged_path = save_path / "handeye3d_result_tool.json"
    merged_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    result["merged_calib"] = str(merged_path)

    out = save_path / "tool_result.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    result["saved_to"] = str(out)
    return result


# --------------- 指尖尖点标定（pivot：多姿态触同一固定点，只用 FK） ---------------


def _load_pivot_samples() -> list[dict]:
    items = []
    for f in sorted(_pivot_dir().glob("*.json")):
        try:
            items.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return items


@app.get("/api/pivot/samples")
async def api_pivot_samples():
    items = _load_pivot_samples()
    return {"samples": items, "count": len(items), "min_samples": MIN_SAMPLES_PIVOT}


@app.post("/api/pivot/samples")
async def api_pivot_add(body: dict | None = None):
    """记录一个尖点样本 = 当前手腕位姿（指尖此刻顶着那个固定点）。

    默认自动读 DDS 位姿；也接受手填 {"T_base_wrist": 4x4} / {"wrist_xyz","wrist_rpy"}。
    """
    body = body or {}
    try:
        if "T_base_wrist" in body or "wrist_xyz" in body:
            T = _parse_wrist_pose(body)
        else:
            if not pose_provider.available:
                return JSONResponse(
                    {"ok": False, "error": f"pose source '{pose_provider.source}' 不支持自动读取，请手填"},
                    status_code=409)
            T = np.asarray(await asyncio.to_thread(pose_provider.read_pose),
                           dtype=float).reshape(4, 4)
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"读取手腕位姿失败: {exc}"}, status_code=503)

    used = [int(p.stem) for p in _pivot_dir().glob("*.json") if p.stem.isdigit()]
    index = (max(used) + 1) if used else 0
    record = {
        "index": index,
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "T_base_wrist": T.tolist(),
        "pose_source": pose_provider.source,
    }
    (_pivot_dir() / f"{index:04d}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False))
    return {"ok": True, "index": index, "count": len(_load_pivot_samples())}


@app.delete("/api/pivot/samples/{index}")
async def api_pivot_delete(index: int):
    f = _pivot_dir() / f"{index:04d}.json"
    if not f.exists():
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    f.unlink()
    return {"ok": True, "count": len(_load_pivot_samples())}


@app.post("/api/pivot/clear")
async def api_pivot_clear():
    for f in _pivot_dir().glob("*.json"):
        f.unlink()
    return {"ok": True, "count": 0}


@app.post("/api/pivot/solve")
async def api_pivot_solve():
    samples = _load_pivot_samples()
    if len(samples) < MIN_SAMPLES_PIVOT:
        return JSONResponse(
            {"ok": False, "error": f"尖点标定至少 {MIN_SAMPLES_PIVOT} 个姿态，当前 {len(samples)} 个"},
            status_code=400)
    T_wrist = np.array([s["T_base_wrist"] for s in samples])
    try:
        result = await asyncio.to_thread(solve_pivot, T_wrist)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    loo = await asyncio.to_thread(leave_one_out_pivot, T_wrist)
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

    # 与现有手眼标定的 p_tool 对比（若有），并生成一份"替换了 p_tool 的完整
    # 标定文件"（handeye3d_result_pivot.json），可直接给 reach_server --calib 用
    handeye = _find_latest_calib()
    if handeye is not None:
        try:
            he = json.loads(handeye.read_text())
            old = np.asarray(he.get("p_tool_wrist_m", []), dtype=float)
            new = np.asarray(result["p_tool_wrist_m"], dtype=float)
            if old.shape == (3,):
                result["delta_vs_handeye_mm"] = ((new - old) * 1000.0).tolist()
                result["delta_vs_handeye_norm_mm"] = float(np.linalg.norm(new - old) * 1000.0)
            he["p_tool_wrist_m"] = result["p_tool_wrist_m"]
            he["p_tool_source"] = "pivot"
            he["pivot_solved_at"] = result["solved_at"]
            merged = save_path / "handeye3d_result_pivot.json"
            merged.write_text(json.dumps(he, indent=2, ensure_ascii=False))
            result["merged_calib"] = str(merged)
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    out = save_path / "pivot_result.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    result["saved_to"] = str(out)
    return result


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

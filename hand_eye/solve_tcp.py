#!/usr/bin/env python3
"""Solve tool-center-point (TCP) calibration by the 4-point *pivot* method.

Setup
-----
A rigid pointed tip is mounted on the suction tool (along the suction axis). The
operator touches ONE fixed reference point in space with that tip from many
different arm orientations, recording the right-arm joint vector each time (no
camera, no board). Because the physical contact point does not move, the tool
offset ``p_tool`` (expressed in the flange frame) must satisfy, for every pose i:

    R_i @ p_tool + t_i = P_world   (a constant point in the base frame)

where ``(R_i, t_i)`` is the forward-kinematics pose of the flange link. Stacking
all poses gives an over-determined linear system in the 6 unknowns
``[p_tool (3); P_world (3)]``, solved by least squares. The per-pose residual
``||R_i p_tool + t_i - P_world||`` measures how well the fixed-point assumption
held and is the calibration quality metric.

Frame
-----
FK is computed to ``--flange-link`` (default ``right_wrist_yaw_link``, the real
physical end link, free of the URDF's modeled dex1 gripper offsets). The result
``p_tool`` is therefore the tool offset RELATIVE TO that link.

Two-point axis (for the suction approach direction)
---------------------------------------------------
Pivot a single point gives only its position. To recover the suction *approach
axis*, capture a second group of poses touching the SAME reference point with a
second point along the needle (group ``p2``). Both groups must resolve to the
same world point (cross-check printed). The axis in the flange frame is then:

    axis = normalize(p_tip - p_p2)            # points outward, from p2 -> tip

and the suction contact (face) center is:

    p_face = p_tip - tip_to_face_mm/1000 * axis

Example
-------
    python solve_tcp.py \
        --data ./handeye_data/20260616_180000 \
        --urdf ../../g1_d.urdf \
        --flange-link right_wrist_yaw_link \
        --tip-group tip --axis-group p2 --tip-to-face-mm 0
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

# Make repo-root modules importable (urdf_robot_model).
# solve_tcp.py: parents [0]=handeye [1]=scripts [2]=vision_arm_control(repo root)
_HANDEYE_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HANDEYE_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_REPO_ROOT), str(_SCRIPTS_DIR), str(_HANDEYE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# --------------- helpers ---------------


def reorder_q(q: np.ndarray, stored_names: List[str], model_names: List[str]) -> np.ndarray:
    """Reorder a captured joint vector into the URDF model's active-joint order."""
    if not stored_names:
        if q.size != len(model_names):
            raise ValueError(f"q size {q.size} != model joints {len(model_names)} and no names to map")
        return q
    index = {n: i for i, n in enumerate(stored_names)}
    out = np.empty(len(model_names), dtype=float)
    for i, name in enumerate(model_names):
        if name not in index:
            raise ValueError(f"Captured joints missing '{name}'. stored={stored_names}")
        out[i] = q[index[name]]
    return out


def solve_pivot(Rs: List[np.ndarray], ts: List[np.ndarray]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Least-squares pivot: find p_tool (flange frame) and P_world (base frame).

    Returns (p_tool, P_world, residuals_m) where residuals_m[i] = ||R_i p_tool + t_i - P_world||.
    """
    n = len(Rs)
    A = np.zeros((3 * n, 6))
    b = np.zeros(3 * n)
    for i, (R, t) in enumerate(zip(Rs, ts)):
        A[3 * i : 3 * i + 3, 0:3] = R
        A[3 * i : 3 * i + 3, 3:6] = -np.eye(3)
        b[3 * i : 3 * i + 3] = -np.asarray(t, dtype=float).reshape(3)
    x, *_ = np.linalg.lstsq(A, b, rcond=None)
    p_tool = x[:3]
    P_world = x[3:]
    res = np.array([np.linalg.norm(R @ p_tool + t - P_world) for R, t in zip(Rs, ts)])
    return p_tool, P_world, res


def load_groups(data_dir: Path, model_joint_names: List[str], model) -> Dict[str, dict]:
    """Load tcp/*.json, FK each pose to the flange, grouped by label."""
    tcp_dir = data_dir / "tcp"
    if not tcp_dir.exists():
        raise SystemExit(f"{data_dir} has no tcp/ folder (capture TCP poses first).")

    groups: Dict[str, dict] = defaultdict(lambda: {"R": [], "t": [], "idx": []})
    for jf in sorted(tcp_dir.glob("*.json")):
        try:
            rec = json.loads(jf.read_text())
        except (OSError, json.JSONDecodeError):
            print(f"  [{jf.name}] SKIP unreadable")
            continue
        q = np.asarray(rec.get("q_rad", []), dtype=float).reshape(-1)
        stored = rec.get("joint_names", [])
        try:
            q_ordered = reorder_q(q, list(stored), model_joint_names)
        except ValueError as exc:
            print(f"  [{jf.name}] SKIP {exc}")
            continue
        p, R = model.fk(q_ordered)
        g = str(rec.get("group", "tip"))
        groups[g]["R"].append(np.asarray(R, dtype=float))
        groups[g]["t"].append(np.asarray(p, dtype=float).reshape(3))
        groups[g]["idx"].append(rec.get("index"))
    return groups


# --------------- main ---------------


def main() -> int:
    parser = argparse.ArgumentParser(description="TCP pivot calibration solver")
    parser.add_argument("--data", required=True, help="Capture session dir (has tcp/)")
    parser.add_argument("--urdf", required=True, help="Robot URDF path")
    parser.add_argument("--base-link", default="torso_link", help="FK base frame")
    parser.add_argument("--flange-link", default="right_wrist_yaw_link",
                        help="Link the tool is mounted on; TCP offset is relative to this")
    parser.add_argument("--tip-group", default="tip",
                        help="Group label of the needle-tip pivot poses (the TCP point)")
    parser.add_argument("--axis-group", default="p2",
                        help="Group label of the second point along the tool axis (for approach axis)")
    parser.add_argument("--tip-to-face-mm", type=float, default=0.0,
                        help="Distance from the needle tip back to the suction face along the axis (mm). "
                             "0 = report the tip itself as the TCP.")
    args = parser.parse_args()

    data_dir = Path(args.data)

    from urdf_robot_model import URDFRobotModel
    model = URDFRobotModel(args.urdf, args.base_link, args.flange_link)
    print(f"[fk] base={args.base_link} flange={args.flange_link} joints={model.joint_names}")

    groups = load_groups(data_dir, model.joint_names, model)
    if not groups:
        raise SystemExit("No usable TCP records found.")

    # Solve a pivot per group.
    solved: Dict[str, dict] = {}
    print("\n" + "=" * 60)
    for g, data in groups.items():
        n = len(data["R"])
        if n < 3:
            print(f"[group '{g}'] only {n} poses (need >= 3, recommend >= 4) -> SKIP")
            continue
        p_tool, P_world, res = solve_pivot(data["R"], data["t"])
        res_mm = res * 1000.0
        solved[g] = {"p_tool": p_tool, "P_world": P_world, "res_mm": res_mm, "n": n}
        print(f"[group '{g}'] {n} poses")
        print(f"   offset in {args.flange_link} (m) = "
              f"[{p_tool[0]:+.5f}, {p_tool[1]:+.5f}, {p_tool[2]:+.5f}]")
        print(f"   touched point in {args.base_link} (m) = "
              f"[{P_world[0]:+.4f}, {P_world[1]:+.4f}, {P_world[2]:+.4f}]")
        print(f"   pivot residual: mean={res_mm.mean():.2f}mm  max={res_mm.max():.2f}mm")
        if res_mm.mean() > 3.0:
            print("   [WARN] residual high -> use more/more-diverse orientations, steadier touch.")
    print("=" * 60)

    if args.tip_group not in solved:
        raise SystemExit(f"Tip group '{args.tip_group}' not solved; cannot report a TCP.")

    p_tip = solved[args.tip_group]["p_tool"]

    result: dict = {
        "base_link": args.base_link,
        "flange_link": args.flange_link,
        "method": "pivot",
        "groups": {
            g: {
                "num_poses": s["n"],
                "offset_in_flange_m": s["p_tool"].tolist(),
                "touched_point_in_base_m": s["P_world"].tolist(),
                "residual_mm": {"mean": float(s["res_mm"].mean()), "max": float(s["res_mm"].max())},
            }
            for g, s in solved.items()
        },
        "tip_group": args.tip_group,
    }

    axis_flange = None
    if args.axis_group in solved and args.axis_group != args.tip_group:
        p_p2 = solved[args.axis_group]["p_tool"]
        vec = p_tip - p_p2
        L = float(np.linalg.norm(vec))
        # Cross-check: both groups should resolve to the same physical world point.
        world_gap_mm = float(np.linalg.norm(solved[args.tip_group]["P_world"]
                                            - solved[args.axis_group]["P_world"]) * 1000.0)
        if L < 1e-6:
            print("[WARN] tip and axis points coincide; cannot define an axis.")
        else:
            axis_flange = vec / L
            print("\nApproach axis (two-point):")
            print(f"   axis in {args.flange_link} = "
                  f"[{axis_flange[0]:+.4f}, {axis_flange[1]:+.4f}, {axis_flange[2]:+.4f}]")
            print(f"   tip<->p2 distance L = {L * 1000.0:.2f} mm")
            print(f"   world-point agreement between groups = {world_gap_mm:.2f} mm "
                  f"({'OK' if world_gap_mm < 5.0 else 'HIGH — recheck touches'})")
            result["approach_axis_in_flange"] = axis_flange.tolist()
            result["tip_to_p2_mm"] = L * 1000.0
            result["world_point_agreement_mm"] = world_gap_mm

    # Suction contact (face) center: tip pulled back along the axis by tip_to_face_mm.
    if axis_flange is not None and args.tip_to_face_mm != 0.0:
        p_face = p_tip - (args.tip_to_face_mm / 1000.0) * axis_flange
    else:
        p_face = p_tip
    result["tip_to_face_mm"] = args.tip_to_face_mm
    result["tcp_offset_in_flange_m"] = p_face.tolist()

    print("\n" + "=" * 60)
    print(f"TCP RESULT  (offset relative to {args.flange_link})")
    print("-" * 60)
    print(f"  suction contact point (m) = "
          f"[{p_face[0]:+.5f}, {p_face[1]:+.5f}, {p_face[2]:+.5f}]")
    print(f"  = [{p_face[0] * 1000:+.1f}, {p_face[1] * 1000:+.1f}, {p_face[2] * 1000:+.1f}] mm")
    if axis_flange is not None:
        print(f"  approach axis (unit)      = "
              f"[{axis_flange[0]:+.4f}, {axis_flange[1]:+.4f}, {axis_flange[2]:+.4f}]")
        print("  (roll about this axis is undefined for a symmetric suction cup)")
    print("=" * 60)

    out_path = data_dir / "tcp_result.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n[OK] saved -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

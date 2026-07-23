#!/usr/bin/env python3
"""
Stereo calibration script.

Usage:
    python -m backend.calibrate --data_path ./data/calib_images/202605261533 --board_size 11x8 --square_size 15

Steps:
    1. Detect chessboard corners in all image pairs
    2. Calibrate left camera intrinsics
    3. Calibrate right camera intrinsics
    4. Stereo calibration (R, T between left and right)
    5. Save results to JSON and YAML
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np


def find_corners(
    image_path: Path, board_size: Tuple[int, int]
) -> Tuple[bool, np.ndarray]:
    """Detect chessboard corners in a single image."""
    img = cv2.imread(str(image_path))
    if img is None:
        return False, None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # NOTE: do NOT use CALIB_CB_LARGER here — it lets the SB detector return a
    # grid bigger than board_size (e.g. 12x8=96 instead of 11x8=88), which then
    # mismatches the fixed object-point count and crashes calibrateCamera.
    found, corners = cv2.findChessboardCornersSB(gray, board_size)
    if found:
        return True, corners
    # Fallback to classic method
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(gray, board_size, flags=flags)
    if found:
        corners = cv2.cornerSubPix(
            gray, corners, (11, 11), (-1, -1),
            (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001),
        )
    return found, corners


def get_object_points(board_size: Tuple[int, int], square_size: float) -> np.ndarray:
    """Generate 3D object points for the chessboard (Z=0 plane)."""
    objp = np.zeros((board_size[0] * board_size[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:board_size[0], 0:board_size[1]].T.reshape(-1, 2)
    objp *= square_size
    return objp


def calibrate_single(
    obj_points: List[np.ndarray],
    img_points: List[np.ndarray],
    image_size: Tuple[int, int],
) -> dict:
    """Calibrate a single camera."""
    ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, image_size, None, None
    )
    return {
        "rms_error": ret,
        "camera_matrix": mtx,
        "dist_coeffs": dist,
        "rvecs": rvecs,
        "tvecs": tvecs,
    }


def stereo_calibrate(
    obj_points: List[np.ndarray],
    left_points: List[np.ndarray],
    right_points: List[np.ndarray],
    left_calib: dict,
    right_calib: dict,
    image_size: Tuple[int, int],
) -> dict:
    """Perform stereo calibration."""
    flags = (
        cv2.CALIB_FIX_INTRINSIC
    )
    ret, M1, d1, M2, d2, R, T, E, F = cv2.stereoCalibrate(
        obj_points,
        left_points,
        right_points,
        left_calib["camera_matrix"],
        left_calib["dist_coeffs"],
        right_calib["camera_matrix"],
        right_calib["dist_coeffs"],
        image_size,
        flags=flags,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6),
    )
    return {
        "rms_error": ret,
        "R": R,
        "T": T,
        "E": E,
        "F": F,
    }


def save_results(output_path: Path, left_calib: dict, right_calib: dict, stereo: dict, image_size, board_size, square_size, log=print):
    """Save calibration results to JSON and OpenCV YAML."""
    output_path.mkdir(parents=True, exist_ok=True)

    # JSON output (human-readable)
    result_json = {
        "resolution": f"{image_size[0]}x{image_size[1]}",  # per-eye, WxH
        "image_size": list(image_size),
        "board_size": list(board_size),
        "square_size_mm": square_size,
        "left_camera": {
            "rms_error": left_calib["rms_error"],
            "camera_matrix": left_calib["camera_matrix"].tolist(),
            "dist_coeffs": left_calib["dist_coeffs"].tolist(),
        },
        "right_camera": {
            "rms_error": right_calib["rms_error"],
            "camera_matrix": right_calib["camera_matrix"].tolist(),
            "dist_coeffs": right_calib["dist_coeffs"].tolist(),
        },
        "stereo": {
            "rms_error": stereo["rms_error"],
            "R": stereo["R"].tolist(),
            "T": stereo["T"].tolist(),
            "E": stereo["E"].tolist(),
            "F": stereo["F"].tolist(),
        },
    }

    json_path = output_path / "calibration_result.json"
    with open(json_path, "w") as f:
        json.dump(result_json, f, indent=2)
    log(f"[OK] JSON results saved to: {json_path}")

    # OpenCV FileStorage YAML (for C++/Python direct loading)
    yaml_path = output_path / "stereo_calibration.yaml"
    fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_WRITE)
    fs.write("image_width", image_size[0])
    fs.write("image_height", image_size[1])
    fs.write("left_camera_matrix", left_calib["camera_matrix"])
    fs.write("left_dist_coeffs", left_calib["dist_coeffs"])
    fs.write("right_camera_matrix", right_calib["camera_matrix"])
    fs.write("right_dist_coeffs", right_calib["dist_coeffs"])
    fs.write("R", stereo["R"])
    fs.write("T", stereo["T"])
    fs.write("E", stereo["E"])
    fs.write("F", stereo["F"])
    fs.release()
    log(f"[OK] OpenCV YAML saved to: {yaml_path}")


def run_calibration(data_path, board_size: Tuple[int, int], square_size: float, log=print) -> dict:
    """Run the full stereo calibration pipeline on a captured session directory.

    Args:
        data_path: session dir containing left/ and right/ subdirs
        board_size: chessboard inner corners (cols, rows)
        square_size: square side length in mm
        log: callable receiving progress strings

    Returns:
        Summary dict (JSON-serializable). Raises ValueError on bad input.
    """
    data_path = Path(data_path)
    left_dir = data_path / "left"
    right_dir = data_path / "right"
    if not left_dir.exists() or not right_dir.exists():
        raise ValueError(f"left/ or right/ directory not found in {data_path}")

    left_files = sorted(left_dir.glob("*.jpg"))
    right_files = sorted(right_dir.glob("*.jpg"))
    if len(left_files) != len(right_files):
        log(f"[WARNING] Left ({len(left_files)}) and right ({len(right_files)}) image counts differ!")

    pairs = list(zip(left_files, right_files))
    log(f"Board size: {board_size[0]}x{board_size[1]}, square size: {square_size} mm")
    log(f"Found {len(pairs)} image pairs in {data_path}")

    objp = get_object_points(board_size, square_size)
    obj_points = []
    left_img_points = []
    right_img_points = []
    image_size = None

    for i, (lf, rf) in enumerate(pairs):
        l_found, l_corners = find_corners(lf, board_size)
        r_found, r_corners = find_corners(rf, board_size)

        expected = objp.shape[0]
        count_ok = (
            l_found and r_found
            and l_corners is not None and r_corners is not None
            and l_corners.shape[0] == expected
            and r_corners.shape[0] == expected
        )

        if count_ok:
            # Align right corner ordering to left (symmetric boards may flip).
            lcf = l_corners.reshape(-1, 2)
            rcf = r_corners.reshape(-1, 2)
            d_same = np.mean(np.linalg.norm(lcf - rcf, axis=1))
            d_rev = np.mean(np.linalg.norm(lcf - rcf[::-1], axis=1))
            if d_rev < d_same:
                r_corners = r_corners[::-1].copy()

            obj_points.append(objp)
            left_img_points.append(l_corners)
            right_img_points.append(r_corners)
            status = "OK"
            if image_size is None:
                img = cv2.imread(str(lf))
                h, w = img.shape[:2]
                image_size = (w, h)
        elif l_found and r_found:
            lc = 0 if l_corners is None else l_corners.shape[0]
            rc = 0 if r_corners is None else r_corners.shape[0]
            status = f"SKIP corner count L={lc} R={rc} != {expected}"
        else:
            parts = []
            if not l_found:
                parts.append("left FAIL")
            if not r_found:
                parts.append("right FAIL")
            status = ", ".join(parts)

        log(f"[{i:03d}] {lf.name} → {status}")

    log(f"Valid pairs: {len(obj_points)} / {len(pairs)}")
    if len(obj_points) < 3:
        raise ValueError(f"Not enough valid pairs for calibration: {len(obj_points)} (need at least 3)")

    log(f"Image size: {image_size[0]}x{image_size[1]}")

    log("Calibrating LEFT camera...")
    left_calib = calibrate_single(obj_points, left_img_points, image_size)
    log(f"  LEFT RMS: {left_calib['rms_error']:.4f} px")

    log("Calibrating RIGHT camera...")
    right_calib = calibrate_single(obj_points, right_img_points, image_size)
    log(f"  RIGHT RMS: {right_calib['rms_error']:.4f} px")

    log("Stereo calibration...")
    stereo = stereo_calibrate(
        obj_points, left_img_points, right_img_points,
        left_calib, right_calib, image_size,
    )
    baseline_mm = float(np.linalg.norm(stereo["T"]))
    log(f"  STEREO RMS: {stereo['rms_error']:.4f} px, baseline: {baseline_mm:.2f} mm")

    save_results(data_path, left_calib, right_calib, stereo, image_size, board_size, square_size, log=log)
    log("Calibration complete!")

    return {
        "valid_pairs": len(obj_points),
        "total_pairs": len(pairs),
        "resolution": f"{image_size[0]}x{image_size[1]}",
        "image_size": list(image_size),
        "left_rms": float(left_calib["rms_error"]),
        "right_rms": float(right_calib["rms_error"]),
        "stereo_rms": float(stereo["rms_error"]),
        "baseline_mm": baseline_mm,
        "yaml_path": str(data_path / "stereo_calibration.yaml"),
        "json_path": str(data_path / "calibration_result.json"),
    }


def main():
    parser = argparse.ArgumentParser(description="Stereo Camera Calibration")
    parser.add_argument("--data_path", required=True, help="Path to calibration data (contains left/ and right/)")
    parser.add_argument("--board_size", default="11x8", help="Inner corners, e.g. 11x8")
    parser.add_argument("--square_size", type=float, default=15.0, help="Square side length in mm")
    args = parser.parse_args()

    cols, rows = args.board_size.split("x")
    try:
        run_calibration(Path(args.data_path), (int(cols), int(rows)), args.square_size, log=print)
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

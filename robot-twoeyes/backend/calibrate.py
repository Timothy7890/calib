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


def save_results(output_path: Path, left_calib: dict, right_calib: dict, stereo: dict, image_size, board_size, square_size):
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
    print(f"[OK] JSON results saved to: {json_path}")

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
    print(f"[OK] OpenCV YAML saved to: {yaml_path}")


def main():
    parser = argparse.ArgumentParser(description="Stereo Camera Calibration")
    parser.add_argument("--data_path", required=True, help="Path to calibration data (contains left/ and right/)")
    parser.add_argument("--board_size", default="11x8", help="Inner corners, e.g. 11x8")
    parser.add_argument("--square_size", type=float, default=15.0, help="Square side length in mm")
    args = parser.parse_args()

    data_path = Path(args.data_path)
    left_dir = data_path / "left"
    right_dir = data_path / "right"

    if not left_dir.exists() or not right_dir.exists():
        print(f"[ERROR] left/ or right/ directory not found in {data_path}")
        sys.exit(1)

    cols, rows = args.board_size.split("x")
    board_size = (int(cols), int(rows))
    square_size = args.square_size

    print(f"Board size: {board_size[0]}x{board_size[1]} inner corners")
    print(f"Square size: {square_size} mm")
    print(f"Data path: {data_path}")
    print()

    # Collect image pairs
    left_files = sorted(left_dir.glob("*.jpg"))
    right_files = sorted(right_dir.glob("*.jpg"))

    if len(left_files) != len(right_files):
        print(f"[WARNING] Left ({len(left_files)}) and right ({len(right_files)}) image counts differ!")
    
    pairs = list(zip(left_files, right_files))
    print(f"Found {len(pairs)} image pairs")
    print()

    # Detect corners
    objp = get_object_points(board_size, square_size)
    obj_points = []
    left_img_points = []
    right_img_points = []
    used_pairs = []
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

        status = ""
        if count_ok:
            # findChessboardCornersSB can order corners starting from either end
            # of a symmetric board, so the same physical corner may land at index
            # 0 in one camera and index N-1 in the other. That breaks the L<->R
            # point correspondence stereoCalibrate relies on (huge stereo RMS
            # even when each mono calibration is fine). With a small baseline the
            # board projects to nearly the same place in both images, so align
            # right ordering to left by reversing it when that matches better.
            lcf = l_corners.reshape(-1, 2)
            rcf = r_corners.reshape(-1, 2)
            d_same = np.mean(np.linalg.norm(lcf - rcf, axis=1))
            d_rev = np.mean(np.linalg.norm(lcf - rcf[::-1], axis=1))
            if d_rev < d_same:
                r_corners = r_corners[::-1].copy()

            obj_points.append(objp)
            left_img_points.append(l_corners)
            right_img_points.append(r_corners)
            used_pairs.append((lf, rf))
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

        print(f"  [{i:03d}] {lf.name} / {rf.name} → {status}")

    print()
    print(f"Valid pairs: {len(obj_points)} / {len(pairs)}")

    if len(obj_points) < 3:
        print("[ERROR] Not enough valid pairs for calibration (need at least 3)")
        sys.exit(1)

    print(f"Image size: {image_size[0]}x{image_size[1]}")
    print()

    # Calibrate left
    print("=" * 50)
    print("Calibrating LEFT camera...")
    left_calib = calibrate_single(obj_points, left_img_points, image_size)
    print(f"  RMS reprojection error: {left_calib['rms_error']:.4f} px")
    print(f"  fx={left_calib['camera_matrix'][0,0]:.2f}, fy={left_calib['camera_matrix'][1,1]:.2f}")
    print(f"  cx={left_calib['camera_matrix'][0,2]:.2f}, cy={left_calib['camera_matrix'][1,2]:.2f}")
    print()

    # Calibrate right
    print("Calibrating RIGHT camera...")
    right_calib = calibrate_single(obj_points, right_img_points, image_size)
    print(f"  RMS reprojection error: {right_calib['rms_error']:.4f} px")
    print(f"  fx={right_calib['camera_matrix'][0,0]:.2f}, fy={right_calib['camera_matrix'][1,1]:.2f}")
    print(f"  cx={right_calib['camera_matrix'][0,2]:.2f}, cy={right_calib['camera_matrix'][1,2]:.2f}")
    print()

    # Stereo calibration
    print("Stereo calibration...")
    stereo = stereo_calibrate(
        obj_points, left_img_points, right_img_points,
        left_calib, right_calib, image_size,
    )
    print(f"  RMS reprojection error: {stereo['rms_error']:.4f} px")
    print(f"  Baseline (T): [{stereo['T'][0,0]:.2f}, {stereo['T'][1,0]:.2f}, {stereo['T'][2,0]:.2f}] mm")
    baseline_mm = np.linalg.norm(stereo['T'])
    print(f"  Baseline distance: {baseline_mm:.2f} mm")
    print()

    # Save
    print("=" * 50)
    save_results(data_path, left_calib, right_calib, stereo, image_size, board_size, square_size)
    print()
    print("Calibration complete!")


if __name__ == "__main__":
    main()

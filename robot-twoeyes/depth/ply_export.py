"""Export colored point cloud as PLY from depth map + RGB + intrinsics."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
import numpy as np


def depth_to_pointcloud(
    rgb: np.ndarray,
    depth_mm: np.ndarray,
    K: np.ndarray,
    radial_mm: Optional[np.ndarray] = None,
    max_depth_mm: float = 5000.0,
) -> np.ndarray:
    """
    Convert RGB image + depth map to point cloud.

    Args:
        rgb: (H, W, 3) BGR image
        depth_mm: (H, W) float32 Z-depth in mm (used for XYZ back-projection)
        K: 3x3 camera intrinsic matrix
        radial_mm: (H, W) float32 radial distance in mm (optional scalar field)
        max_depth_mm: discard points beyond this depth

    Returns:
        points: (N, 7) array [x, y, z, r, g, b, radial] if radial provided,
                else (N, 6) [x, y, z, r, g, b]
    """
    h, w = depth_mm.shape
    fx, fy = K[0, 0], K[1, 1]
    cx, cy = K[0, 2], K[1, 2]

    u, v = np.meshgrid(np.arange(w), np.arange(h))

    valid = (depth_mm > 0) & (depth_mm < max_depth_mm)

    z = depth_mm[valid]
    x = (u[valid] - cx) * z / fx
    y = (v[valid] - cy) * z / fy

    colors = rgb[valid][:, ::-1]  # BGR → RGB

    cols = [x, y, z, colors[:, 0], colors[:, 1], colors[:, 2]]
    if radial_mm is not None:
        cols.append(radial_mm[valid])

    points = np.column_stack(cols).astype(np.float32)
    return points


def save_ply(filepath: str, points: np.ndarray, has_radial: bool = False):
    """
    Save point cloud as PLY file.

    Args:
        filepath: output .ply path
        points: (N, 6 or 7) array
        has_radial: if True, 7th column is written as scalar field "depth"
    """
    n = len(points)
    header = (
        "ply\n"
        "format ascii 1.0\n"
        f"element vertex {n}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "property uchar red\n"
        "property uchar green\n"
        "property uchar blue\n"
    )
    if has_radial:
        header += "property float depth\n"
    header += "end_header\n"

    with open(filepath, "w") as f:
        f.write(header)
        if has_radial:
            for p in points:
                f.write(f"{p[0]:.3f} {p[1]:.3f} {p[2]:.3f} "
                        f"{int(p[3])} {int(p[4])} {int(p[5])} {p[6]:.3f}\n")
        else:
            for p in points:
                f.write(f"{p[0]:.3f} {p[1]:.3f} {p[2]:.3f} "
                        f"{int(p[3])} {int(p[4])} {int(p[5])}\n")


def export_pointcloud(
    rgb: np.ndarray,
    depth_mm: np.ndarray,
    K: np.ndarray,
    output_path: str,
    radial_mm: Optional[np.ndarray] = None,
    max_depth_mm: float = 5000.0,
) -> int:
    """
    Full pipeline: RGB + depth → PLY file.

    Returns:
        Number of points exported.
    """
    points = depth_to_pointcloud(rgb, depth_mm, K, radial_mm, max_depth_mm)
    has_radial = radial_mm is not None
    save_ply(output_path, points, has_radial)
    return len(points)

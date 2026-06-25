"""Stereo rectification, SGBM disparity, and depth map generation."""

from __future__ import annotations

import base64
from typing import Tuple

import cv2
import numpy as np
import requests

HAS_XIMGPROC = hasattr(cv2, 'ximgproc')


class StereoDepthProcessor:
    """Loads calibration and computes depth maps from stereo pairs.

    Supports pluggable disparity methods ('sgbm' or 'crestereo') and
    optional WLS post-filtering.
    """

    def __init__(self, calib_path: str, method: str = "sgbm"):
        self.method = method
        self._load_calibration(calib_path)
        self._compute_rectification()
        if method == "sgbm":
            self._create_sgbm()
        elif method == "crestereo":
            self._init_crestereo()

    def _load_calibration(self, calib_path: str):
        fs = cv2.FileStorage(calib_path, cv2.FILE_STORAGE_READ)
        self.image_width = int(fs.getNode("image_width").real())
        self.image_height = int(fs.getNode("image_height").real())
        self.M1 = fs.getNode("left_camera_matrix").mat()
        self.d1 = fs.getNode("left_dist_coeffs").mat()
        self.M2 = fs.getNode("right_camera_matrix").mat()
        self.d2 = fs.getNode("right_dist_coeffs").mat()
        self.R = fs.getNode("R").mat()
        self.T = fs.getNode("T").mat()
        fs.release()
        self.image_size = (self.image_width, self.image_height)

    def _compute_rectification(self):
        self.R1, self.R2, self.P1, self.P2, self.Q, _, _ = cv2.stereoRectify(
            self.M1, self.d1, self.M2, self.d2,
            self.image_size, self.R, self.T,
            alpha=0,
            flags=cv2.CALIB_ZERO_DISPARITY,
        )
        self.map1_left, self.map2_left = cv2.initUndistortRectifyMap(
            self.M1, self.d1, self.R1, self.P1, self.image_size, cv2.CV_16SC2
        )
        self.map1_right, self.map2_right = cv2.initUndistortRectifyMap(
            self.M2, self.d2, self.R2, self.P2, self.image_size, cv2.CV_16SC2
        )
        self.focal = self.P1[0, 0]
        self.baseline = abs(self.T[0, 0])  # mm

    # ---- SGBM ----

    def _create_sgbm(self):
        num_disparities = 64
        block_size = 5
        self.sgbm_left = cv2.StereoSGBM_create(
            minDisparity=0,
            numDisparities=num_disparities,
            blockSize=block_size,
            P1=24 * 3 * block_size ** 2,
            P2=96 * 3 * block_size ** 2,
            disp12MaxDiff=1,
            uniquenessRatio=15,
            speckleWindowSize=100,
            speckleRange=32,
            preFilterCap=63,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
        )
        # Right matcher for WLS filtering
        if HAS_XIMGPROC:
            self.sgbm_right = cv2.ximgproc.createRightMatcher(self.sgbm_left)
            self.wls_filter = cv2.ximgproc.createDisparityWLSFilter(
                matcher_left=self.sgbm_left,
            )
            self.wls_filter.setLambda(8000.0)
            self.wls_filter.setSigmaColor(1.5)
        else:
            self.sgbm_right = None
            self.wls_filter = None

    def _compute_disparity_sgbm(
        self, left_rect: np.ndarray, right_rect: np.ndarray, use_wls: bool = True,
    ) -> np.ndarray:
        left_gray = cv2.cvtColor(left_rect, cv2.COLOR_BGR2GRAY)
        right_gray = cv2.cvtColor(right_rect, cv2.COLOR_BGR2GRAY)

        disp_left = self.sgbm_left.compute(left_gray, right_gray)

        if use_wls and HAS_XIMGPROC and self.wls_filter is not None:
            disp_right = self.sgbm_right.compute(right_gray, left_gray)
            disp_filtered = self.wls_filter.filter(
                disp_left, left_gray, None, disp_right,
            )
            return disp_filtered.astype(np.float32) / 16.0

        if use_wls and not HAS_XIMGPROC:
            # Fallback: median filter on disparity
            disp = disp_left.astype(np.float32) / 16.0
            valid_mask = disp > 0
            disp_med = cv2.medianBlur(
                disp.astype(np.float32), 5,
            )
            disp_med[~valid_mask] = 0
            return disp_med

        return disp_left.astype(np.float32) / 16.0

    # ---- CREStereo (via HTTP service at port 8126) ----

    CRESTEREO_URL = "http://localhost:8126/api/disparity"

    def _init_crestereo(self):
        print("[StereoDepth] CREStereo mode: will call HTTP service at", self.CRESTEREO_URL)

    def _encode_jpeg_b64(self, img: np.ndarray) -> str:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError("Failed to encode image")
        return base64.b64encode(buf.tobytes()).decode("ascii")

    def _compute_disparity_crestereo(
        self, left_rect: np.ndarray, right_rect: np.ndarray, use_wls: bool = False,
    ) -> np.ndarray:
        h, w = left_rect.shape[:2]
        payload = {
            "left": self._encode_jpeg_b64(left_rect),
            "right": self._encode_jpeg_b64(right_rect),
            "height": h,
            "width": w,
        }
        resp = requests.post(
            self.CRESTEREO_URL, json=payload, timeout=120,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()

        disp_bytes = base64.b64decode(data["disparity"])
        disp = np.frombuffer(disp_bytes, dtype=np.float32).reshape(
            data["height"], data["width"],
        )
        return disp

    # ---- Public API ----

    def rectify(self, left: np.ndarray, right: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Rectify a stereo pair."""
        left_rect = cv2.remap(left, self.map1_left, self.map2_left, cv2.INTER_LINEAR)
        right_rect = cv2.remap(right, self.map1_right, self.map2_right, cv2.INTER_LINEAR)
        return left_rect, right_rect

    def compute_disparity(
        self, left_rect: np.ndarray, right_rect: np.ndarray, use_wls: bool = True,
    ) -> np.ndarray:
        """Compute disparity map (float, in pixels)."""
        if self.method == "crestereo":
            return self._compute_disparity_crestereo(left_rect, right_rect, use_wls)
        return self._compute_disparity_sgbm(left_rect, right_rect, use_wls)

    def disparity_to_depth(self, disparity: np.ndarray) -> np.ndarray:
        """Convert disparity to depth in mm. Invalid pixels are set to 0."""
        depth = np.zeros_like(disparity, dtype=np.float32)
        valid = disparity > 0
        depth[valid] = (self.focal * self.baseline) / disparity[valid]
        return depth

    def z_depth_to_radial(self, z_depth: np.ndarray) -> np.ndarray:
        """Convert Z-depth map to radial distance map (Euclidean distance from optical center)."""
        h, w = z_depth.shape
        K = self.get_left_intrinsics()
        fx, fy = K[0, 0], K[1, 1]
        cx, cy = K[0, 2], K[1, 2]

        u, v = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        radial = z_depth * np.sqrt(1.0 + ((u - cx) / fx) ** 2 + ((v - cy) / fy) ** 2)
        radial[z_depth <= 0] = 0
        return radial

    def compute_depth(
        self, left: np.ndarray, right: np.ndarray, use_wls: bool = True,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Full pipeline: rectify -> disparity -> radial distance.
        Returns (left_rectified, radial_distance_mm, z_depth_mm).
        """
        left_rect, right_rect = self.rectify(left, right)
        disparity = self.compute_disparity(left_rect, right_rect, use_wls)
        z_depth = self.disparity_to_depth(disparity)
        radial = self.z_depth_to_radial(z_depth)
        return left_rect, radial, z_depth

    def depth_to_colormap(
        self, depth: np.ndarray, min_depth_mm: float = 200.0, max_depth_mm: float = 1000.0
    ) -> np.ndarray:
        """
        Convert depth to red-blue colormap visualization.
        Near = red, far = blue, invalid/out-of-range = black.
        """
        valid = (depth > min_depth_mm) & (depth < max_depth_mm)
        normalized = np.zeros_like(depth, dtype=np.uint8)
        if valid.any():
            d = depth[valid]
            normalized[valid] = ((d - min_depth_mm) / (max_depth_mm - min_depth_mm) * 255).astype(np.uint8)

        colored = cv2.applyColorMap(normalized, cv2.COLORMAP_JET)
        colored[~valid] = [0, 0, 0]
        return colored

    def get_left_intrinsics(self) -> np.ndarray:
        """Return rectified left camera matrix (from P1)."""
        K = np.eye(3, dtype=np.float64)
        K[0, 0] = self.P1[0, 0]
        K[1, 1] = self.P1[1, 1]
        K[0, 2] = self.P1[0, 2]
        K[1, 2] = self.P1[1, 2]
        return K

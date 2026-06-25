"""Chessboard corner detection (mirrors robot-twoeyes detection style)."""

from __future__ import annotations

from typing import Tuple

import cv2
import numpy as np


def parse_board_size(size_str: str) -> Tuple[int, int]:
    """Parse '11x8' style string into (cols, rows) inner-corner counts."""
    parts = size_str.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"Invalid board size format: {size_str}, expected 'COLSxROWS'")
    return int(parts[0]), int(parts[1])


def detect_corners(
    image: np.ndarray,
    board_size: Tuple[int, int] = (11, 8),
    draw: bool = False,
) -> Tuple[bool, np.ndarray]:
    """Detect chessboard inner corners. Returns (found, image_with_optional_overlay)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # NOTE: no CALIB_CB_LARGER — it lets the SB detector return a grid bigger
    # than board_size (e.g. 12x8 instead of 11x8), which destabilizes the
    # detected corner count. Keep detection strict to board_size.
    found, corners = cv2.findChessboardCornersSB(gray, board_size)

    result = image.copy()
    if found and draw:
        cv2.drawChessboardCorners(result, board_size, corners, found)
    return found, result

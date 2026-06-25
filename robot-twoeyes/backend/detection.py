"""Chessboard corner detection utilities."""

import cv2
import numpy as np


def parse_board_size(size_str: str) -> tuple[int, int]:
    """Parse '9x6' style string into (cols, rows) tuple."""
    parts = size_str.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"Invalid board size format: {size_str}, expected 'COLSxROWS'")
    return int(parts[0]), int(parts[1])


def detect_corners(
    image: np.ndarray,
    board_size: tuple[int, int] = (9, 6),
    draw: bool = False,
) -> tuple[bool, np.ndarray]:
    """
    Detect chessboard corners in an image.

    Returns:
        (detected, result_image)
        - detected: whether corners were found
        - result_image: image with corners drawn if draw=True, otherwise original
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Use SB (sector-based) method — more robust and stable
    found, corners = cv2.findChessboardCornersSB(gray, board_size, flags=cv2.CALIB_CB_LARGER)

    result = image.copy()
    if found and draw:
        cv2.drawChessboardCorners(result, board_size, corners, found)

    return found, result

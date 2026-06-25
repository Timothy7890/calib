"""Head camera interface for hand-eye capture.

Wraps the Unitree G1 head stereo camera (teleimager ImageClient) and returns
the LEFT half of the side-by-side head frame, which is the eye used by the
cigarette perception pipeline. A mock source is provided so the web UI can be
exercised without the robot.
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Tuple

import cv2
import numpy as np


class HeadCamera:
    """Persistent connection to the head stereo camera, left/right split."""

    def __init__(self, source: str = "teleimager", host: str = "127.0.0.1"):
        self.source = source
        self._host = host
        self._client = None
        self._lock = threading.Lock()
        self._mock_t0 = time.monotonic()
        if source == "teleimager":
            self._connect()
        elif source == "mock":
            pass
        else:
            raise ValueError(f"Unknown camera source: {source!r} (use 'teleimager' or 'mock').")

    def _connect(self) -> None:
        from teleimager.image_client import ImageClient

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = ImageClient(host=self._host)

    def grab_left(self, timeout: float = 2.0) -> Optional[np.ndarray]:
        """Grab a single left-eye image. Returns None on failure."""
        pair = self.grab_pair(timeout=timeout)
        if pair is None:
            return None
        return pair[0]

    def grab_pair(self, timeout: float = 2.0) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Grab a single (left, right) pair. Returns None on failure."""
        if self.source == "mock":
            return self._mock_pair()

        with self._lock:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    frame, _fps = self._client.get_head_frame()
                except Exception:
                    self._connect()
                    time.sleep(0.1)
                    continue

                if frame is not None:
                    h, w = frame.shape[:2]
                    if w % 2 != 0:
                        return None
                    left = frame[:, : w // 2].copy()
                    right = frame[:, w // 2 :].copy()
                    return left, right
                time.sleep(0.05)
        return None

    def _mock_pair(self) -> Tuple[np.ndarray, np.ndarray]:
        """Synthetic moving target so the stream/UI works without hardware."""
        h, w = 480, 640
        img = np.full((h, w, 3), 30, dtype=np.uint8)
        t = time.monotonic() - self._mock_t0
        cx = int(w / 2 + 120 * np.cos(t))
        cy = int(h / 2 + 80 * np.sin(t * 0.7))
        cv2.rectangle(img, (cx - 60, cy - 40), (cx + 60, cy + 40), (200, 200, 200), -1)
        cv2.putText(img, "MOCK LEFT", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 212, 255), 2)
        right = img.copy()
        cv2.putText(right, "MOCK RIGHT", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 212, 255), 2)
        return img, right

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

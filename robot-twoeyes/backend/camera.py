"""Camera interface wrapper — keeps a persistent ImageClient connection."""

from __future__ import annotations

import time
import threading
from typing import Optional, Tuple

import numpy as np


class CameraManager:
    """Long-lived wrapper around teleimager ImageClient for the head stereo camera."""

    def __init__(self, host: str = "127.0.0.1"):
        self._host = host
        self._client = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        from teleimager.image_client import ImageClient
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = ImageClient(host=self._host)

    def grab(self, timeout: float = 2.0) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Grab a single stereo pair (left, right). Returns None on failure."""
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
                    left = frame[:, :w // 2].copy()
                    right = frame[:, w // 2:].copy()
                    return left, right
                time.sleep(0.05)
        return None

    def close(self):
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

"""CREStereo ONNX stereo disparity estimator.

Adapted from ibaiGorordo/ONNX-CREStereo-Depth-Estimation.

Model naming convention (PINTO_model_zoo #284):
  crestereo_init_iter{N}_{H}x{W}.onnx        — single pass (2 inputs)
  crestereo_combined_iter{N}_{H}x{W}.onnx     — two-pass   (4 inputs)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
import onnxruntime as ort


class CREStereoEstimator:
    """CREStereo ONNX inference wrapper."""

    def __init__(
        self,
        model_path: str,
        providers: Optional[List[str]] = None,
    ):
        if providers is None:
            providers = ort.get_available_providers()

        self.session = ort.InferenceSession(model_path, providers=providers)

        model_inputs = self.session.get_inputs()
        self.input_names = [inp.name for inp in model_inputs]

        model_outputs = self.session.get_outputs()
        self.output_names = [out.name for out in model_outputs]

        # combined model has 4 inputs; init model has 2
        self.has_flow = len(self.input_names) > 2

        # Full-resolution shape from the LAST input (combined: inputs 2,3 are full-res)
        full_shape = model_inputs[-1].shape
        self.model_h = full_shape[2]
        self.model_w = full_shape[3]

        self.inf_time = 0.0

        mode = "combined (two-pass)" if self.has_flow else "init (single-pass)"
        print(f"[CREStereo] Loaded: {Path(model_path).name} ({mode})")
        print(f"  Input size : {self.model_w}x{self.model_h}")
        print(f"  Inputs     : {self.input_names}")
        print(f"  Providers  : {self.session.get_providers()}")

    def _prepare_input(self, img_bgr: np.ndarray, half: bool = False) -> np.ndarray:
        """BGR -> RGB, resize, HWC -> NCHW float32 (pixel values 0~255)."""
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

        if half:
            target = (self.model_w // 2, self.model_h // 2)
        else:
            target = (self.model_w, self.model_h)

        resized = cv2.resize(rgb, target, interpolation=cv2.INTER_AREA)
        return resized.transpose(2, 0, 1)[np.newaxis].astype(np.float32)

    def estimate(self, left_bgr: np.ndarray, right_bgr: np.ndarray) -> np.ndarray:
        """Compute disparity map from a rectified stereo pair.

        Args:
            left_bgr:  Left rectified image (H, W, 3) BGR uint8
            right_bgr: Right rectified image (H, W, 3) BGR uint8

        Returns:
            Disparity map (orig_H, orig_W) float32, in pixels at original resolution.
        """
        orig_h, orig_w = left_bgr.shape[:2]

        left_tensor = self._prepare_input(left_bgr)
        right_tensor = self._prepare_input(right_bgr)

        start = time.monotonic()

        if self.has_flow:
            # Combined: 4 inputs — half_left, half_right, full_left, full_right
            left_half = self._prepare_input(left_bgr, half=True)
            right_half = self._prepare_input(right_bgr, half=True)

            output = self.session.run(
                self.output_names,
                {
                    self.input_names[0]: left_half,
                    self.input_names[1]: right_half,
                    self.input_names[2]: left_tensor,
                    self.input_names[3]: right_tensor,
                },
            )[0]
        else:
            # Init: 2 inputs — left, right
            output = self.session.run(
                self.output_names,
                {
                    self.input_names[0]: left_tensor,
                    self.input_names[1]: right_tensor,
                },
            )[0]

        self.inf_time = time.monotonic() - start

        # output shape: [1, 2, H, W] — channel 0 is disparity
        disp = np.squeeze(output[:, 0, :, :])  # (model_h, model_w)

        # Scale to original resolution
        disp_resized = cv2.resize(disp, (orig_w, orig_h))
        scale_x = float(orig_w) / float(self.model_w)
        disp_resized *= scale_x

        return disp_resized

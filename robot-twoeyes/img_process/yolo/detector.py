"""YOLO11 ONNX detector — handles preprocessing, inference, postprocessing, and mask decoding."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
import onnxruntime as ort


COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

# Fixed color palette for visualization (BGR)
_COLORS = [
    (56, 56, 255), (151, 157, 255), (31, 112, 255), (29, 178, 255),
    (49, 210, 207), (10, 249, 72), (23, 204, 146), (134, 219, 61),
    (182, 210, 57), (243, 218, 11), (255, 173, 0), (255, 113, 0),
    (255, 56, 56), (255, 0, 131), (186, 0, 221), (117, 29, 255),
]


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


class YOLODetector:
    """YOLO11 ONNX inference wrapper with segmentation mask support.

    Supports both detection and segmentation models:
      - Detection: output0 [1, (4+num_classes), 8400]
      - Segment:   output0 [1, (4+num_classes+32), 8400] + output1 [1, 32, 160, 160]
    """

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        class_names: Optional[List[str]] = None,
        providers: Optional[List[str]] = None,
    ):
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold

        if providers is None:
            providers = ort.get_available_providers()
        self.session = ort.InferenceSession(model_path, providers=providers)

        meta = self.session.get_inputs()[0]
        self.input_name = meta.name
        _, self.channels, self.input_h, self.input_w = meta.shape

        outputs = self.session.get_outputs()
        out_dim = outputs[0].shape[1]

        self.is_seg = len(outputs) >= 2 and outputs[1].shape[1] == 32
        self.num_mask_coeffs = 32 if self.is_seg else 0
        self.num_classes = out_dim - 4 - self.num_mask_coeffs

        if class_names is not None:
            self.class_names = class_names
        elif self.num_classes == 80:
            self.class_names = COCO_NAMES
        else:
            self.class_names = [f"class_{i}" for i in range(self.num_classes)]

        model_type = "segment" if self.is_seg else "detect"
        print(f"[YOLODetector] Loaded: {Path(model_path).name} ({model_type})")
        print(f"  Input : {self.input_name} [1, {self.channels}, {self.input_h}, {self.input_w}]")
        print(f"  Classes: {self.num_classes} {self.class_names}")
        print(f"  Providers: {self.session.get_providers()}")

    # ---- preprocessing ----

    def preprocess(self, img_bgr: np.ndarray) -> Tuple[np.ndarray, float, Tuple[int, int]]:
        """Letterbox resize + normalize. Returns (blob, scale, (pad_x, pad_y))."""
        h0, w0 = img_bgr.shape[:2]
        scale = min(self.input_w / w0, self.input_h / h0)
        new_w, new_h = int(w0 * scale), int(h0 * scale)

        resized = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        pad_x = (self.input_w - new_w) // 2
        pad_y = (self.input_h - new_h) // 2
        padded = np.full((self.input_h, self.input_w, 3), 114, dtype=np.uint8)
        padded[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized

        blob = padded[:, :, ::-1].astype(np.float32) / 255.0
        blob = blob.transpose(2, 0, 1)[np.newaxis]
        return blob, scale, (pad_x, pad_y)

    # ---- postprocessing (detection) ----

    def _decode_boxes(
        self,
        output0: np.ndarray,
        scale: float,
        pad: Tuple[int, int],
        orig_shape: Tuple[int, int],
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, Optional[np.ndarray]]:
        """Shared box decoding: conf filter + NMS.

        Returns (boxes_xyxy, class_ids, scores, nms_indices, mask_coeffs_or_None).
        All arrays are pre-NMS filtered by confidence; use nms_indices to index them.
        """
        preds = output0[0].T  # [num_boxes, 4+C+32]

        boxes_cxcywh = preds[:, :4]
        scores = preds[:, 4:4 + self.num_classes]
        mask_coeffs = preds[:, 4 + self.num_classes:] if self.is_seg else None

        class_ids = scores.argmax(axis=1)
        max_scores = scores[np.arange(len(scores)), class_ids]

        keep = max_scores >= self.conf_threshold
        boxes_cxcywh = boxes_cxcywh[keep]
        class_ids = class_ids[keep]
        max_scores = max_scores[keep]
        if mask_coeffs is not None:
            mask_coeffs = mask_coeffs[keep]

        if len(boxes_cxcywh) == 0:
            empty = np.empty((0, 4), dtype=np.float32)
            return empty, np.array([]), np.array([]), np.array([]), None

        x1 = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
        y1 = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
        x2 = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
        y2 = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2

        pad_x, pad_y = pad
        x1 = (x1 - pad_x) / scale
        y1 = (y1 - pad_y) / scale
        x2 = (x2 - pad_x) / scale
        y2 = (y2 - pad_y) / scale

        h0, w0 = orig_shape
        x1 = np.clip(x1, 0, w0)
        y1 = np.clip(y1, 0, h0)
        x2 = np.clip(x2, 0, w0)
        y2 = np.clip(y2, 0, h0)
        boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

        offset = class_ids.astype(np.float32) * 7680
        nms_boxes = boxes_xyxy.copy()
        nms_boxes[:, 0] += offset
        nms_boxes[:, 1] += offset
        nms_boxes[:, 2] += offset
        nms_boxes[:, 3] += offset

        indices = cv2.dnn.NMSBoxes(
            nms_boxes.tolist(), max_scores.tolist(),
            self.conf_threshold, self.iou_threshold,
        )
        if len(indices) == 0:
            empty = np.empty((0, 4), dtype=np.float32)
            return empty, np.array([]), np.array([]), np.array([]), None
        indices = np.array(indices).flatten()

        return boxes_xyxy, class_ids, max_scores, indices, mask_coeffs

    # ---- mask decoding ----

    def _decode_masks(
        self,
        mask_coeffs: np.ndarray,
        protos: np.ndarray,
        boxes_xyxy: np.ndarray,
        indices: np.ndarray,
        orig_shape: Tuple[int, int],
        scale: float,
        pad: Tuple[int, int],
    ) -> List[np.ndarray]:
        """Decode segmentation masks for NMS-selected detections.

        Returns list of binary masks, each of shape (orig_h, orig_w), dtype uint8 (0 or 255).
        """
        # protos: [1, 32, mask_h, mask_w] → [32, mask_h*mask_w]
        proto = protos[0]  # [32, 160, 160]
        mask_h, mask_w = proto.shape[1], proto.shape[2]
        proto_flat = proto.reshape(32, -1)  # [32, 160*160]

        selected_coeffs = mask_coeffs[indices]  # [N, 32]

        # Matrix multiply: [N, 32] @ [32, H*W] → [N, H*W] then sigmoid
        raw_masks = _sigmoid(selected_coeffs @ proto_flat)
        raw_masks = raw_masks.reshape(-1, mask_h, mask_w)  # [N, 160, 160]

        h0, w0 = orig_shape
        pad_x, pad_y = pad

        # Scale factors: mask space (160x160) vs input space (640x640)
        mask_scale_x = mask_w / self.input_w
        mask_scale_y = mask_h / self.input_h

        results = []
        for i, idx in enumerate(indices):
            mask_160 = raw_masks[i]

            # Crop mask to letterboxed content region (remove padding area)
            pad_x_m = int(pad_x * mask_scale_x)
            pad_y_m = int(pad_y * mask_scale_y)
            content_w = int((self.input_w - 2 * pad_x) * mask_scale_x)
            content_h = int((self.input_h - 2 * pad_y) * mask_scale_y)
            cropped = mask_160[pad_y_m:pad_y_m + content_h, pad_x_m:pad_x_m + content_w]

            if cropped.size == 0:
                results.append(np.zeros((h0, w0), dtype=np.uint8))
                continue

            # Resize to original image size
            full_mask = cv2.resize(cropped, (w0, h0), interpolation=cv2.INTER_LINEAR)

            # Crop to bounding box for cleaner edges
            bx1, by1, bx2, by2 = boxes_xyxy[idx].astype(int)
            bx1, by1 = max(0, bx1), max(0, by1)
            bx2, by2 = min(w0, bx2), min(h0, by2)
            bbox_mask = np.zeros((h0, w0), dtype=np.float32)
            bbox_mask[by1:by2, bx1:bx2] = full_mask[by1:by2, bx1:bx2]

            binary = (bbox_mask > 0.5).astype(np.uint8) * 255
            results.append(binary)

        return results

    def _masks_to_contours(self, masks: List[np.ndarray]) -> List[List[List[int]]]:
        """Convert binary masks to contour point lists for JSON serialization.

        Returns list of contour arrays. Each contour: [[x,y], [x,y], ...].
        """
        all_contours = []
        for mask in masks:
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                largest = max(contours, key=cv2.contourArea)
                pts = largest.reshape(-1, 2).tolist()
                all_contours.append(pts)
            else:
                all_contours.append([])
        return all_contours

    # ---- main entry ----

    def detect(self, img_bgr: np.ndarray) -> List[dict]:
        """Run detection (bbox only). Returns list of detections."""
        blob, scale, pad = self.preprocess(img_bgr)
        outputs = self.session.run(None, {self.input_name: blob})

        boxes, class_ids, scores, indices, _ = self._decode_boxes(
            outputs[0], scale, pad, img_bgr.shape[:2],
        )
        if len(indices) == 0:
            return []

        results = []
        for i in indices:
            results.append({
                "bbox": [round(float(v), 1) for v in boxes[i]],
                "confidence": round(float(scores[i]), 4),
                "class_id": int(class_ids[i]),
                "class_name": self.class_names[class_ids[i]],
            })
        return results

    def detect_with_masks(self, img_bgr: np.ndarray) -> Tuple[List[dict], List[np.ndarray]]:
        """Run detection + segmentation.

        Returns (detections, masks).
          - detections: list of dicts with bbox, confidence, class_id, class_name, contour
          - masks: list of binary mask arrays (h, w), uint8, 0 or 255
        """
        blob, scale, pad = self.preprocess(img_bgr)
        outputs = self.session.run(None, {self.input_name: blob})

        boxes, class_ids, scores, indices, mask_coeffs = self._decode_boxes(
            outputs[0], scale, pad, img_bgr.shape[:2],
        )
        if len(indices) == 0:
            return [], []

        masks = []
        contours = []
        if self.is_seg and mask_coeffs is not None:
            masks = self._decode_masks(
                mask_coeffs, outputs[1], boxes, indices,
                img_bgr.shape[:2], scale, pad,
            )
            contours = self._masks_to_contours(masks)

        results = []
        for idx_pos, i in enumerate(indices):
            det = {
                "bbox": [round(float(v), 1) for v in boxes[i]],
                "confidence": round(float(scores[i]), 4),
                "class_id": int(class_ids[i]),
                "class_name": self.class_names[class_ids[i]],
            }
            if contours:
                det["contour"] = contours[idx_pos]
            results.append(det)

        return results, masks

    def detect_and_draw(
        self, img_bgr: np.ndarray, draw_masks: bool = True,
    ) -> Tuple[np.ndarray, List[dict]]:
        """Detect + draw bounding boxes and masks. Returns (annotated_image, detections)."""
        if self.is_seg and draw_masks:
            detections, masks = self.detect_with_masks(img_bgr)
        else:
            detections = self.detect(img_bgr)
            masks = []

        vis = img_bgr.copy()
        overlay = vis.copy()

        for idx, det in enumerate(detections):
            color = _COLORS[det["class_id"] % len(_COLORS)]
            x1, y1, x2, y2 = [int(v) for v in det["bbox"]]

            # Draw mask
            if idx < len(masks) and masks[idx] is not None:
                mask_bool = masks[idx] > 127
                overlay[mask_bool] = color

            # Draw bbox
            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
            label = f'{det["class_name"]} {det["confidence"]:.2f}'
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(vis, (x1, y1 - th - 6), (x1 + tw, y1), color, -1)
            cv2.putText(vis, label, (x1, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        # Blend mask overlay
        if masks:
            vis = cv2.addWeighted(overlay, 0.4, vis, 0.6, 0)

        return vis, detections

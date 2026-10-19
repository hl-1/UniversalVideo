from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from pathlib import Path

from backend.core.paths import ROOT_DIR as PROJECT_ROOT


MODEL_PATH = PROJECT_ROOT / ".run" / "models" / "lama_fp32.onnx"
MODEL_SIZE = 208044816
_session = None
_session_lock = threading.Lock()
_inference_lock = threading.Lock()


def device_label() -> str:
    if _session is None:
        return "等待初始化"
    providers = _session.get_providers()
    if "CUDAExecutionProvider" in providers:
        return "NVIDIA GPU"
    if "DmlExecutionProvider" in providers:
        return "DirectML GPU"
    return "CPU"


def _cpu_session():
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"])


def availability() -> dict:
    dependencies = all(importlib.util.find_spec(name) is not None for name in ("numpy", "cv2", "onnxruntime"))
    available = dependencies and MODEL_PATH.is_file() and MODEL_PATH.stat().st_size == MODEL_SIZE
    return {"available": available, "message": "LaMa 模型已就绪" if available else "AI 修复模型未安装", "device": device_label()}


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            if not availability()["available"]:
                raise RuntimeError("请先安装 AI 修复依赖，并运行 scripts/setup-watermark-model.py。")
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            providers = ["CPUExecutionProvider"]
            device = os.environ.get("VIDEODREAM_INPAINTING_DEVICE", "auto")
            available_providers = ort.get_available_providers()
            if device in {"auto", "cuda"} and "CUDAExecutionProvider" in available_providers:
                try:
                    ort.preload_dlls()
                    providers.insert(0, ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}))
                except Exception:
                    logging.getLogger(__name__).warning("CUDA libraries unavailable; using CPU inpainting", exc_info=True)
            elif device in {"auto", "dml"} and "DmlExecutionProvider" in available_providers:
                options.enable_mem_pattern = False
                providers.insert(0, ("DmlExecutionProvider", {"device_id": os.environ.get("VIDEODREAM_DML_DEVICE_ID", "0")}))
            try:
                _session = ort.InferenceSession(str(MODEL_PATH), sess_options=options, providers=providers)
            except Exception:
                if len(providers) == 1:
                    raise
                logging.getLogger(__name__).warning("GPU session unavailable; using CPU inpainting", exc_info=True)
                _session = _cpu_session()
        return _session


def repair_frame(frame, regions, timing: dict | None = None):
    global _session
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        mask[region.y:region.y + region.height, region.x:region.x + region.width] = 255
    points = cv2.findNonZero(mask)
    if points is None:
        return frame.copy()
    x, y, w, h = cv2.boundingRect(points)
    margin = max(64, round(max(w, h) * 0.5))
    x1, y1, x2, y2 = max(0, x - margin), max(0, y - margin), min(width, x + w + margin), min(height, y + h + margin)
    crop, crop_mask = frame[y1:y2, x1:x2], mask[y1:y2, x1:x2]
    crop_height, crop_width = crop.shape[:2]
    scale = min(1.0, 512 / max(crop_height, crop_width))
    resized_width, resized_height = max(1, round(crop_width * scale)), max(1, round(crop_height * scale))
    image = cv2.resize(crop, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
    # Hide compression halos from the model, while compositing inside the original mask.
    inference_mask = cv2.dilate(crop_mask, np.ones((9, 9), dtype=np.uint8))
    small_mask = cv2.resize(inference_mask, (resized_width, resized_height), interpolation=cv2.INTER_NEAREST)
    image = cv2.copyMakeBorder(image, 0, 512 - resized_height, 0, 512 - resized_width, cv2.BORDER_REFLECT_101)
    small_mask = cv2.copyMakeBorder(small_mask, 0, 512 - resized_height, 0, 512 - resized_width, cv2.BORDER_CONSTANT)
    inputs = {
        "image": np.ascontiguousarray(image.transpose(2, 0, 1)[None], dtype=np.float32) / 255,
        "mask": np.ascontiguousarray((small_mask > 0)[None, None], dtype=np.float32),
    }
    with _inference_lock:
        session = _get_session()
        try:
            started = time.perf_counter()
            result = session.run(None, inputs)
        except Exception:
            if not {"DmlExecutionProvider", "CUDAExecutionProvider"}.intersection(session.get_providers()):
                raise
            logging.getLogger(__name__).warning("GPU inference failed; using CPU inpainting", exc_info=True)
            with _session_lock:
                _session = _cpu_session()
            started = time.perf_counter()
            result = _session.run(None, inputs)
        if timing is not None:
            timing["seconds"] = time.perf_counter() - started
        prediction = result[0][0].transpose(1, 2, 0)
    prediction = np.clip(prediction[:resized_height, :resized_width], 0, 255).astype(np.uint8)
    restored = cv2.resize(prediction, (crop_width, crop_height), interpolation=cv2.INTER_CUBIC)
    # Keep unmasked pixels exact; feather only the inner two pixels of the mask.
    alpha = np.minimum(cv2.distanceTransform(crop_mask, cv2.DIST_L2, 3) / 2, 1)[..., None]
    blended = np.rint(restored.astype(np.float32) * alpha + crop.astype(np.float32) * (1 - alpha)).astype(np.uint8)
    output = frame.copy()
    output[y1:y2, x1:x2] = blended
    return output

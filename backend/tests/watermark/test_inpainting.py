import importlib.util
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from backend.core import paths
from backend.modules.watermark import inpainting
from backend.modules.watermark import api as watermark
from backend.tests.watermark import test_watermark as fixtures
from backend.infrastructure.runtime import executable_name


class InpaintingValidationTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("onnxruntime"), "Optional inference runtime is required")
    def test_nvidia_provider_is_selected_and_libraries_are_preloaded(self):
        import onnxruntime as ort

        session = MagicMock()
        session.get_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        with patch.object(inpainting, "_session", None), patch.object(inpainting, "availability", return_value={"available": True}), \
                patch.dict("os.environ", {"VIDEODREAM_INPAINTING_DEVICE": "auto"}), \
                patch.object(ort, "get_available_providers", return_value=["CUDAExecutionProvider", "CPUExecutionProvider"]), \
                patch.object(ort, "preload_dlls", create=True) as preload, patch.object(ort, "InferenceSession", return_value=session) as create:
            self.assertIs(inpainting._get_session(), session)
            preload.assert_called_once()
            self.assertEqual(create.call_args.kwargs["providers"][0][0], "CUDAExecutionProvider")
            self.assertEqual(inpainting.device_label(), "NVIDIA GPU")

    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("onnxruntime", "cv2", "numpy")), "Optional inference dependencies are required")
    def test_gpu_inference_failure_falls_back_to_cpu_and_stays_on_cpu(self):
        import numpy as np

        gpu, cpu = MagicMock(), MagicMock()
        gpu.get_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        gpu.run.side_effect = RuntimeError("GPU memory unavailable")
        cpu.get_providers.return_value = ["CPUExecutionProvider"]
        cpu.run.return_value = [np.full((1, 3, 512, 512), 64, dtype=np.float32)]
        frame = np.full((180, 320, 3), 100, dtype=np.uint8)
        region = watermark.WatermarkRegion(x=59, y=29, width=52, height=32)
        with patch.object(inpainting, "_session", gpu), patch.object(inpainting, "_cpu_session", return_value=cpu) as fallback, self.assertLogs(inpainting.__name__, level="WARNING"):
            repaired = inpainting.repair_frame(frame, [region])
            inpainting.repair_frame(frame, [region])
            fallback.assert_called_once()
            gpu.run.assert_called_once()
            self.assertEqual(inpainting.device_label(), "CPU")
            self.assertEqual(repaired[45, 85].tolist(), [64, 64, 64])
            np.testing.assert_array_equal(repaired[0, 0], frame[0, 0])

    def test_uninstalled_model_is_reported_without_loading_session(self):
        with patch.object(inpainting, "MODEL_SIZE", -1):
            self.assertFalse(inpainting.availability()["available"])

    def test_cancel_stops_both_decoder_and_encoder(self):
        from unittest.mock import MagicMock

        encoder, decoder = MagicMock(), MagicMock()
        encoder.poll.return_value = decoder.poll.return_value = None
        watermark._stop_processes(type("Job", (), {"process": encoder, "decoder": decoder})())
        encoder.kill.assert_called_once()
        decoder.kill.assert_called_once()


@unittest.skipUnless(inpainting.availability()["available"], "Local LaMa weights and optional dependencies are required")
class LaMaProcessingTests(unittest.TestCase):
    setUp = fixtures.WatermarkTests.setUp
    tearDown = fixtures.WatermarkTests.tearDown

    @classmethod
    def setUpClass(cls):
        inpainting._get_session()

    def _make_video(self):
        subprocess.run([
            str(watermark.FFMPEG_DIR / executable_name("ffmpeg")), "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x204030:size=320x180:rate=5:duration=0.8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8", "-vf", "drawbox=x=60:y=30:w=50:h=30:color=white:t=fill",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.source),
        ], check=True, capture_output=True)

    def test_lama_preserves_unmasked_pixels_exactly(self):
        import numpy as np

        frame = np.full((180, 320, 3), [32, 64, 48], dtype=np.uint8)
        frame[30:60, 60:110] = 255
        original = frame.copy()
        result = inpainting.repair_frame(frame, [self.region])
        mask = np.zeros(frame.shape[:2], dtype=bool)
        mask[29:61, 59:111] = True
        np.testing.assert_array_equal(result[~mask], frame[~mask])
        np.testing.assert_array_equal(frame, original)
        self.assertLess(max(result[45, 85]), 100)

    def test_lama_preview_uses_actual_frame_and_provides_cost(self):
        import cv2
        import numpy as np

        self._make_video()
        response = watermark.preview_watermark(watermark.WatermarkPreviewRequest(filename=self.source.name, regions=[self.region], method="lama", time=0.4))
        frame = cv2.imdecode(np.frombuffer(response.body, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(frame.shape, (180, 320, 3))
        self.assertLess(max(frame[45, 85]), 100)
        self.assertGreater(float(response.headers["X-Processing-Seconds"]), 0)
        with self.assertRaises(HTTPException) as raised:
            watermark.preview_watermark(watermark.WatermarkPreviewRequest(filename=self.source.name, regions=[self.region], method="lama", time=3))
        self.assertEqual(raised.exception.status_code, 422)

    def test_lama_full_video_keeps_audio_duration_and_original(self):
        self._make_video()
        original = self.source.read_bytes()
        info = watermark._probe_video(self.source)
        job = watermark.WatermarkJob("lama", self.source, info, [self.region], method="lama")
        watermark._run_watermark(job)
        self.assertEqual(job.status, "finished", job.error)
        self.assertEqual(self.source.read_bytes(), original)
        output = self.root / job.filename
        result_info = watermark._probe_video(output)
        self.assertEqual((result_info.width, result_info.height), (320, 180))
        self.assertAlmostEqual(result_info.duration, info.duration, delta=0.2)
        from json import loads
        result = subprocess.run([str(watermark.FFMPEG_DIR / executable_name("ffprobe")), "-v", "error", "-show_streams", "-of", "json", str(output)], check=True, capture_output=True)
        self.assertIn("audio", [stream["codec_type"] for stream in loads(result.stdout)["streams"]])

    def test_cancel_during_ai_processing_cleans_partial_and_keeps_source(self):
        self._make_video()
        info = watermark._probe_video(self.source)
        job = watermark.WatermarkJob("cancel-ai", self.source, info, [self.region], method="lama")
        watermark.jobs[job.task_id] = job
        processing, resume = threading.Event(), threading.Event()

        def pause(frame, regions):
            processing.set()
            resume.wait(5)
            return frame

        with patch.object(inpainting, "repair_frame", side_effect=pause):
            worker = threading.Thread(target=watermark._run_watermark, args=(job,), daemon=True)
            worker.start()
            self.assertTrue(processing.wait(5))
            watermark.cancel_watermark(job.task_id)
            resume.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(list(self.root.iterdir()), [self.source])


if __name__ == "__main__":
    unittest.main()

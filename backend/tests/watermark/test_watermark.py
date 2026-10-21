import hashlib
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from backend.core import paths, storage
from backend.modules.watermark import api as watermark
from backend.infrastructure.runtime import executable_name


class WatermarkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.directory.name)
        self.root = self.storage_root / "users" / "1" / "files"
        self.root.mkdir(parents=True)
        for mocked in (patch.object(paths, "DOWNLOAD_DIR", self.storage_root),
                       patch.object(storage, "owner", side_effect=lambda uid=None: uid or 1)):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.path_patch = patch.object(watermark, "DOWNLOAD_DIR", self.root)
        self.path_patch.start()
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"original video")
        self.info = watermark.VideoInfo(filename=self.source.name, width=320, height=180, duration=1, preview_url="/api/watermarks/videos/source.mp4")
        self.region = watermark.WatermarkRegion(x=59, y=29, width=52, height=32)
        with watermark.jobs_lock:
            watermark.jobs.clear()

    def tearDown(self):
        self.path_patch.stop()
        # Windows may briefly retain a file handle after cancelling ffmpeg.
        for attempt in range(5):
            try:
                self.directory.cleanup()
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * 2 ** attempt)

    def test_source_paths_cannot_escape_downloads(self):
        for name in ("../source.mp4", "..\\source.mp4", "C:\\source.mp4", "source.mp4:secret", "https://example.com/file.mp4"):
            with self.subTest(name=name), self.assertRaises(HTTPException) as raised:
                watermark._video_path(name)
            self.assertEqual(raised.exception.status_code, 422)

    def test_audio_and_missing_files_are_rejected(self):
        for name, code in (("audio.m4a", 422), ("missing.mp4", 404)):
            with self.subTest(name=name), self.assertRaises(HTTPException) as raised:
                watermark._video_path(name)
            self.assertEqual(raised.exception.status_code, code)

    def test_regions_cannot_touch_outside_border(self):
        for region in (
            watermark.WatermarkRegion(x=300, y=20, width=20, height=10),
            watermark.WatermarkRegion(x=20, y=170, width=20, height=10),
        ):
            with self.assertRaises(HTTPException) as raised:
                watermark._validate_regions([region], self.info)
            self.assertEqual(raised.exception.status_code, 422)
        watermark._validate_regions([watermark.WatermarkRegion(x=1, y=1, width=318, height=178)], self.info)

    def test_corrupt_video_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            watermark._probe_video(self.source)
        self.assertEqual(raised.exception.status_code, 422)

    def test_rotated_video_uses_display_dimensions(self):
        probe = {"streams": [{"width": 320, "height": 180, "sample_aspect_ratio": "1:1", "side_data_list": [{"rotation": 90}]}], "format": {"duration": "1"}}
        result = subprocess.CompletedProcess([], 0, json.dumps(probe), "")
        with patch.object(watermark.subprocess, "run", return_value=result):
            info = watermark._probe_video(self.source)
        self.assertEqual((info.width, info.height), (180, 320))

    def test_video_list_excludes_audio_and_incomplete_outputs(self):
        (self.root / "audio.m4a").write_bytes(b"audio")
        (self.root / "incomplete.part.mp4").write_bytes(b"partial")
        self.assertEqual([video["filename"] for video in watermark.list_videos()], ["source.mp4"])

    def test_processed_video_can_link_back_to_original(self):
        (self.root / "aabbccddeeff.source.watermark-removed.mp4").write_bytes(b"processed")
        processed = next(video for video in watermark.list_videos() if video["filename"].endswith("watermark-removed.mp4"))
        self.assertEqual(processed["original_filename"], "source.mp4")

    def test_unknown_average_fps_falls_back_to_real_frame_rate(self):
        probe = {"streams": [{"width": 320, "height": 180, "avg_frame_rate": "0/0", "r_frame_rate": "24/1"}], "format": {"duration": "1"}}
        result = subprocess.CompletedProcess([], 0, json.dumps(probe), "")
        with patch.object(watermark.subprocess, "run", return_value=result):
            self.assertEqual(watermark._probe_video(self.source).fps, 24)

    def test_pending_job_can_be_cancelled_before_processing(self):
        with patch.object(watermark, "_probe_video", return_value=self.info), patch.object(watermark.executor, "submit"):
            response = watermark.create_watermark(watermark.WatermarkRequest(filename="source.mp4", regions=[self.region]))
        cancelled = watermark.cancel_watermark(response.task_id)
        watermark._run_watermark(watermark.jobs[response.task_id])
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(watermark.get_watermark(response.task_id).status, "cancelled")
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_ai_progress_reports_frames_eta_and_subpercent_progress(self):
        info = self.info.model_copy(update={"duration": 100, "fps": 60})
        job = watermark.WatermarkJob("progress", self.source, info, [self.region], method="lama", status="running",
                                     started_at=80, processing_started_at=90, frames_processed=10, progress=10 / 6000 * 100,
                                     processing_device="NVIDIA GPU")
        with patch.object(watermark.time, "perf_counter", return_value=100):
            response = job.response()
        self.assertEqual(response.progress, 0.17)
        self.assertEqual((response.frames_processed, response.total_frames), (10, 6000))
        self.assertEqual(response.elapsed_seconds, 20)
        self.assertEqual(response.remaining_seconds, 5990)
        job.stopped_at, job.status = 105, "cancelled"
        with patch.object(watermark.time, "perf_counter", return_value=1000):
            response = job.response()
        self.assertEqual(response.elapsed_seconds, 25)
        self.assertIsNone(response.remaining_seconds)

    def test_queue_is_bounded(self):
        with patch.object(watermark, "_probe_video", return_value=self.info), patch.object(watermark.executor, "submit"):
            payload = watermark.WatermarkRequest(filename="source.mp4", regions=[self.region])
            for _ in range(4):
                watermark.create_watermark(payload)
            with self.assertRaises(HTTPException) as raised:
                watermark.create_watermark(payload)
        self.assertEqual(raised.exception.status_code, 429)

    def test_processor_failure_preserves_source(self):
        job = watermark.WatermarkJob("failed", self.source, self.info, [self.region])
        with patch.object(watermark.subprocess, "Popen", side_effect=OSError("encoder unavailable")):
            watermark._run_watermark(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.source.read_bytes(), b"original video")
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_running_job_cancellation_stops_encoder_and_removes_partial(self):
        started, killed = threading.Event(), threading.Event()
        process = MagicMock()
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode
        process.wait.side_effect = lambda: process.returncode

        def kill():
            process.returncode = -9
            killed.set()

        def progress():
            storage.path("temp", "running.source.watermark-removed.part.mp4", uid=1).write_bytes(b"incomplete")
            started.set()
            killed.wait(3)
            yield "out_time_us=10000\n"

        process.kill.side_effect = kill
        process.stdout.__iter__.side_effect = progress
        job = watermark.WatermarkJob("running", self.source, self.info, [self.region])
        watermark.jobs[job.task_id] = job
        with patch.object(watermark.subprocess, "Popen", return_value=process):
            worker = threading.Thread(target=watermark._run_watermark, args=(job,), daemon=True)
            worker.start()
            self.assertTrue(started.wait(2))
            watermark.cancel_watermark(job.task_id)
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertFalse(storage.path("temp", "running.source.watermark-removed.part.mp4", uid=1).exists())
        self.assertTrue(killed.is_set())
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_real_processing_removes_logo_preserves_source_and_audio(self):
        ffmpeg = watermark.FFMPEG_DIR / executable_name("ffmpeg")
        subprocess.run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x204030:size=320x180:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-vf", "drawbox=x=60:y=30:w=50:h=30:color=white:t=fill",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.source),
        ], check=True, capture_output=True)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        info = watermark._probe_video(self.source)
        job = watermark.WatermarkJob("integration", self.source, info, [self.region])
        watermark._run_watermark(job)
        self.assertEqual(job.status, "finished", job.error)
        self.assertEqual(job.progress, 100)
        output = self.root / job.filename
        self.assertTrue(output.is_file())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual((watermark._probe_video(output).width, watermark._probe_video(output).height), (320, 180))
        probe = subprocess.run([str(watermark.FFMPEG_DIR / executable_name("ffprobe")), "-v", "error", "-show_streams", "-of", "json", str(output)], check=True, capture_output=True, text=True)
        self.assertIn("audio", [stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]])
        frame = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
        center = (45 * 320 + 85) * 3
        self.assertLess(max(frame[center:center + 3]), 100)
        self.assertFalse(any(path.name.endswith(".part.mp4") for path in self.root.iterdir()))

        rotated = self.root / "rotated.mp4"
        subprocess.run([str(ffmpeg), "-v", "error", "-display_rotation", "90", "-i", str(self.source), "-c", "copy", str(rotated)], check=True, capture_output=True)
        rotated_info = watermark._probe_video(rotated)
        self.assertEqual((rotated_info.width, rotated_info.height), (180, 320))
        rotated_job = watermark.WatermarkJob("portrait", rotated, rotated_info, [watermark.WatermarkRegion(x=29, y=209, width=32, height=52)])
        watermark._run_watermark(rotated_job)
        self.assertEqual(rotated_job.status, "finished", rotated_job.error)
        rotated_output = self.root / rotated_job.filename
        output_info = watermark._probe_video(rotated_output)
        self.assertEqual((output_info.width, output_info.height), (180, 320))
        frame = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(rotated_output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
        center = (235 * 180 + 45) * 3
        self.assertLess(max(frame[center:center + 3]), 100)


if __name__ == "__main__":
    unittest.main()

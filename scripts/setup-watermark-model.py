"""Download the pinned LaMa ONNX weights used by local video inpainting."""

import argparse
import hashlib
import time
from pathlib import Path

import requests


REVISION = "c3c0c9e468934d62e79c329e35d82dd09ff8c444"
SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
SIZE = 208044816
MODEL_PATH = Path(__file__).resolve().parent.parent / ".run" / "models" / "lama_fp32.onnx"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror", action="store_true", help="Use hf-mirror.com for the same pinned weights")
    args = parser.parse_args()
    if MODEL_PATH.is_file():
        with MODEL_PATH.open("rb") as existing:
            if hashlib.file_digest(existing, "sha256").hexdigest() == SHA256:
                print("LaMa model is ready.", flush=True)
                return
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    partial = MODEL_PATH.with_suffix(".onnx.part")
    host = "hf-mirror.com" if args.mirror else "huggingface.co"
    url = f"https://{host}/Carve/LaMa-ONNX/resolve/{REVISION}/lama_fp32.onnx?download=true"
    digest = hashlib.sha256()
    received, last_report = 0, 0
    try:
        with requests.get(url, stream=True, timeout=(20, 60)) as response:
            response.raise_for_status()
            with partial.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if time.monotonic() - last_report > 5:
                        print(f"LaMa model: {received / SIZE:.0%} ({received // 1048576} / {SIZE // 1048576} MiB)", flush=True)
                        last_report = time.monotonic()
        if received != SIZE or digest.hexdigest() != SHA256:
            raise RuntimeError("Model size or SHA-256 does not match the pinned weights.")
        partial.replace(MODEL_PATH)
        print(f"LaMa model verified: {MODEL_PATH}", flush=True)
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

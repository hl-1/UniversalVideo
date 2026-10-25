"""Install the pinned Flyway CLI into the ignored project tools directory."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "13.11.1"
ARCHIVE_URL = f"https://github.com/flyway/flyway/releases/download/flyway-{VERSION}/flyway-commandline-{VERSION}.tar.gz"
ARCHIVE_SHA256 = "c44f9389a6e77e363f7cf25bdbb431f6a8ee16387101ffe1f85e9f16fcd0972e"
DRIVER_URL = "https://repo.maven.apache.org/maven2/com/mysql/mysql-connector-j/9.5.0/mysql-connector-j-9.5.0.jar"
DRIVER_SHA256 = "f2ca3dfaf00d4aa311470db7ea3051962944ba0cb60005a2f75467549c39f425"


def download(url: str, destination: Path, expected: str) -> None:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "VideoDream-Flyway-Setup"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while block := response.read(1024 * 1024):
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != expected:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Download checksum mismatch: " + destination.name)


def main():
    if not shutil.which("java"):
        raise SystemExit("Install Java 21+ before setting up Flyway, or use the Docker migration service.")
    tools = ROOT / ".run" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    target = tools / f"flyway-{VERSION}"
    if target.exists():
        raise SystemExit(f"Already installed: {target}. Remove this tools directory explicitly to reinstall.")
    with tempfile.TemporaryDirectory(prefix="flyway-setup-", dir=tools) as temporary:
        work = Path(temporary)
        print(f"Downloading Flyway {VERSION} (publisher SHA-256 verified)...", flush=True)
        archive = work / "flyway.tar.gz"
        download(ARCHIVE_URL, archive, ARCHIVE_SHA256)
        with tarfile.open(archive) as bundle:
            bundle.extractall(work / "unpacked", filter="data")
        unpacked = work / "unpacked" / f"flyway-{VERSION}"
        download(DRIVER_URL, unpacked / "drivers" / "mysql-connector-j-9.5.0.jar", DRIVER_SHA256)
        (unpacked / "flyway").chmod(0o755)
        shutil.move(str(unpacked), str(target))
    print(f"Installed: {target / ('flyway.cmd' if os.name == 'nt' else 'flyway')}")


if __name__ == "__main__":
    main()

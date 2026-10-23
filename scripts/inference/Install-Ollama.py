"""Download the pinned portable engine inside this workspace; no system install."""
from pathlib import Path
import hashlib
import json
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.40.9"
SHA256 = "c35019c95679eae1cb28fda4732a7c97978b9fbcbd8adfb5cbcc8fe7c161ea0b"
URL = f"https://github.com/ollama/ollama/releases/download/v{VERSION}/ollama-windows-amd64.zip"


def main():
    archive = ROOT / f".local/downloads/ollama-v{VERSION}-windows-amd64.zip"
    target = ROOT / f".local/apps/ollama/v{VERSION}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        partial = archive.with_suffix(".zip.partial")
        started = last = time.monotonic()
        with urllib.request.urlopen(URL, timeout=60) as response, partial.open("wb") as output:
            total = int(response.headers["Content-Length"])
            received = 0
            while block := response.read(4 * 1024 * 1024):
                output.write(block)
                received += len(block)
                if time.monotonic() - last >= 15:
                    print(f"download {received}/{total} bytes ({received / total:.1%})", flush=True)
                    last = time.monotonic()
        if received != total:
            raise RuntimeError("Incomplete archive")
        partial.rename(archive)
        print(f"download_seconds={time.monotonic() - started:.2f}", flush=True)
    digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"Archive checksum mismatch: {digest}")
    if not target.exists():
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                if not (target / member.filename).resolve().is_relative_to(target.resolve()):
                    raise RuntimeError("Archive path escapes engine directory")
            package.extractall(target)
    if not (target / "ollama.exe").is_file():
        raise RuntimeError("Missing engine executable")
    lock = ROOT / ".local/locks/engine-development.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"version": VERSION, "url": URL, "sha256": digest,
                                "archive_bytes": archive.stat().st_size}, indent=2), encoding="utf-8")
    print(f"verified_engine={target}", flush=True)


if __name__ == "__main__":
    main()

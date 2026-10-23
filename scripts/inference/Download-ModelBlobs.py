"""Pinned-version fallback for a stalled native pull, using official blobs.

Run only with the local engine stopped. Every blob and the raw manifest are
checked before publishing the manifest into the portable model directory.
"""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import time
import requests
import socket

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--prefetch-projector", action="store_true")
parser.add_argument("--workers", type=int, choices=(1, 4, 8), default=8)
args = parser.parse_args()
models = ROOT / ".local/ollama/models"
registry = "https://registry.ollama.ai/v2/library/qwen3-vl"
with socket.socket() as probe:
    if probe.connect_ex(("127.0.0.1", 11434)) == 0:
        raise RuntimeError("Stop the owned local engine before writing model files")
manifest_cache = ROOT / ".local/downloads/qwen3vl4b-84f7ed8042fa-manifest.json"
if manifest_cache.exists():
    manifest_raw = manifest_cache.read_bytes()
else:
    for manifest_attempt in range(3):
        try:
            response = requests.get(registry + "/manifests/4b-instruct-q4_K_M", timeout=30)
            response.raise_for_status()
            manifest_raw = response.content
            break
        except requests.RequestException:
            if manifest_attempt == 2:
                raise
            time.sleep(2)
manifest = json.loads(manifest_raw)
digest = hashlib.sha256(manifest_raw).hexdigest()
if digest != "84f7ed8042fa7cdbd902f7dcb7d0370e032095047cc3244179f263b77b5bc318":
    raise RuntimeError(f"Official model tag has changed: {digest}")
manifest_cache.parent.mkdir(parents=True, exist_ok=True)
manifest_cache.write_bytes(manifest_raw)
print("manifest_sha256=" + digest, flush=True)
for layer in [manifest["config"], *manifest["layers"]]:
    if args.prefetch_projector and layer["mediaType"] != "application/vnd.ollama.image.projector":
        continue
    expected = layer["digest"].removeprefix("sha256:")
    dest = models / "blobs" / ("sha256-" + expected)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        with dest.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() == expected:
                print("already_verified=" + expected[:12], flush=True)
                continue
        raise RuntimeError("Existing blob checksum mismatch")
    partial = dest.with_suffix(".prefetch" if args.prefetch_projector else ".download")
    offset = partial.stat().st_size if partial.exists() else 0
    started = last = time.monotonic()
    chunk_dir = ROOT / ".local/downloads/modelparts" / expected
    chunk_dir.mkdir(parents=True, exist_ok=True)
    ranges = [(begin, min(begin + 32 * 1024 * 1024, layer["size"]) - 1)
              for begin in range(offset, layer["size"], 32 * 1024 * 1024)]

    def fetch(bounds):
        begin, end = bounds
        chunk_path = chunk_dir / f"{begin}-{end}.part"
        if chunk_path.exists() and chunk_path.stat().st_size == end - begin + 1:
            return chunk_path
        for attempt in range(3):
            try:
                url = registry + "/blobs/" + layer["digest"] + "?bline=" + str(time.time_ns())
                with requests.get(url, headers={"Range": f"bytes={begin}-{end}"}, stream=True, timeout=(30, 60)) as incoming:
                    incoming.raise_for_status()
                    wanted = f"bytes {begin}-{end}/{layer['size']}"
                    if incoming.status_code != 206 or incoming.headers.get("Content-Range") != wanted:
                        raise RuntimeError("Range not honored")
                    count = 0
                    with chunk_path.open("wb") as output:
                        for block in incoming.iter_content(1024 * 1024):
                            count += len(block)
                            if count > end - begin + 1:
                                raise RuntimeError("Range exceeds expected size")
                            output.write(block)
                    if count != end - begin + 1:
                        raise RuntimeError("Incomplete range")
                return chunk_path
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

    print(f"downloading={expected[:12]} offset={offset} total={layer['size']} workers={args.workers}", flush=True)
    completed_bytes = offset
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch, bounds) for bounds in ranges]
        for future in as_completed(futures):
            completed_bytes += future.result().stat().st_size
            if time.monotonic() - last > 15:
                print(f"{expected[:12]} {completed_bytes}/{layer['size']} ({completed_bytes/layer['size']:.1%})", flush=True)
                last = time.monotonic()
    with partial.open("ab") as output:
        for begin, end in ranges:
            with (chunk_dir / f"{begin}-{end}.part").open("rb") as source:
                while block := source.read(4 * 1024 * 1024):
                    output.write(block)
                    offset += len(block)
    with partial.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if offset != layer["size"] or actual != expected:
        raise RuntimeError("Downloaded blob checksum/length mismatch")
    partial.rename(dest)
    print(f"verified={expected[:12]} seconds={time.monotonic()-started:.2f}", flush=True)
if args.prefetch_projector:
    print("projector_prefetch_verified", flush=True)
    raise SystemExit(0)
path = models / "manifests/registry.ollama.ai/library/qwen3-vl/4b-instruct-q4_K_M"
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists() and path.read_bytes() != manifest_raw:
    raise RuntimeError("Existing manifest differs")
path.write_bytes(manifest_raw)
print("all_blobs_and_manifest_verified", flush=True)

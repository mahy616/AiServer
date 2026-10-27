"""Package the one-click importer; no models, engines or private settings."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FILES = ("Install-Qwen3VL8B.cmd", "scripts/inference/Install-Qwen3VL8B.py",
         "scripts/inference/Prepare-AdditionalModel.py", "scripts/server/manager_support.py",
         "config/inference.5080.8b.example.json", "docs/正式工程运维.md")


def build(root=ROOT, output=None):
    root = Path(root).resolve()
    snapshots = [(name, (root / name).read_bytes()) for name in FILES]
    rows = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in snapshots]
    manifest = {"kind": "qwen3-vl-8b-offline-importer", "formatVersion": 1,
                "requires": "Existing AiServer 0.6.0+ with its working primary model and Python dependencies",
                "files": rows}
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(output or root / ".local/releases")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ("AiServer-Qwen3VL8B-installer-" + stamp + "-" + digest[:8] + ".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as packed:
        for name, data in snapshots:
            packed.writestr(name, data)
        packed.writestr("8B-installer-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with zipfile.ZipFile(archive) as packed:
        if set(packed.namelist()) != set(FILES) | {"8B-installer-manifest.json"} or packed.testzip():
            raise ValueError("Installer archive verification failed")
        for row in rows:
            if hashlib.sha256(packed.read(row["path"])).hexdigest() != row["sha256"]:
                raise ValueError("Installer file checksum mismatch")
    return {"zip": str(archive.resolve()), "bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": len(FILES)}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))

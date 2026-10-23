"""Build source + framework-dependent Demo release, excluding private state."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "releases")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    target = args.output / ("AiServer-0.2.0-" + revision + ".zip")
    if target.exists():
        parser.error("Release already exists; choose a new output directory")
    demo = args.output / ("demo-" + revision)
    subprocess.run(["dotnet", "publish", str(ROOT / "tools/ClientDemo/ClientDemo.csproj"),
                    "-c", "Release", "--self-contained", "false", "-o", str(demo)], check=True)
    files = []
    for directory in ("server/src", "contracts", "scripts", "config", "tools/ClientDemo"):
        for path in (ROOT / directory).rglob("*"):
            relative = path.relative_to(ROOT)
            if (path.is_file() and not any(part in ("__pycache__", "bin", "obj") for part in relative.parts)
                    and path.name not in ("server.local.json", "inference.development.json", "inference.production.json")
                    and path.suffix not in (".token", ".secret", ".pyc", ".db", ".log")):
                files.append((path, relative.as_posix()))
    files += [(ROOT / name, name) for name in ("server/pyproject.toml", "server/requirements.lock.txt", "server/README.md", "docs/正式工程运维.md")]
    manifest = {}
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
        for path, name in files + [(p, "client/" + p.name) for p in demo.iterdir() if p.is_file()]:
            data = path.read_bytes()
            archive.writestr(name, data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        archive.writestr("release-manifest.json", json.dumps({"version": "0.2.0", "revision": revision,
                           "workingTreeDirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
                           "sha256": manifest}, indent=2))
    print(target)

if __name__ == "__main__":
    main()

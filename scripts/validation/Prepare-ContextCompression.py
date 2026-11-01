"""Download only the pinned public assets used by offline compression screening."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[2]


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fetch(client, url, path, expected_hash, expected_size=None):
    if path.is_file() and sha256(path) == expected_hash:
        return "verified-existing"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    print("Downloading", path.name, flush=True)
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with temporary.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
    if sha256(temporary) != expected_hash or expected_size is not None and temporary.stat().st_size != expected_size:
        raise ValueError("Asset checksum or length mismatch: " + path.name)
    temporary.replace(path)
    return "downloaded-verified"


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--root", type=Path, default=ROOT / ".local/context-compression-lab")
    cli.add_argument("--models", nargs="*", choices=("mbert", "xlm-roberta"), default=["mbert"],
                     help="No values: prepare tokenizer/cache only; never download generation models")
    args = cli.parse_args()
    manifest = json.loads((Path(__file__).with_name("context_compression_models.json")).read_text(encoding="utf-8"))
    selected = {"qwen35-tokenizer", *args.models}
    records = []
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120, connect=30)) as client:
        for asset in manifest["assets"]:
            if asset["id"] not in selected:
                continue
            for file in asset["files"]:
                url = f'https://huggingface.co/{asset["repository"]}/resolve/{asset["revision"]}/{quote(file["name"])}'
                path = args.root / asset["directory"] / file["name"]
                status = fetch(client, url, path, file["sha256"], file["size"])
                records.append({"asset": asset["id"], "file": file["name"], "status": status,
                                "sha256": file["sha256"], "revision": asset["revision"]})
        encoding = manifest["tiktoken"]
        status = fetch(client, encoding["url"], args.root / "tiktoken-cache" / encoding["filename"], encoding["sha256"])
        records.append({"asset": "cl100k_base", "status": status, "sha256": encoding["sha256"]})
    (args.root / "asset-verification.json").write_text(json.dumps(records, indent=2), encoding="utf-8", newline="\n")
    print("Verified", len(records), "public assets; no service configuration changed.", flush=True)


if __name__ == "__main__":
    main()

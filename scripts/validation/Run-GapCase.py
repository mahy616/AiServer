"""Small reproducible direct-model experiment, not a second business service."""
from pathlib import Path
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
from aiserver.context.prompt_builder import prepare_prompt
from aiserver.context.input_budget import check_budget
from aiserver.inference.ollama import OllamaAdapter


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sample_gpu(stop, samples):
    while not stop.is_set():
        result = subprocess.run(["C:/Windows/System32/nvidia-smi.exe",
                                 "--query-gpu=memory.used,memory.total,utilization.gpu",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        samples.append({"utc": datetime.now(timezone.utc).isoformat(), "csv": result.stdout.strip()})
        stop.wait(1)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controlled-experiment", action="store_true", required=True,
                        help="Explicitly acknowledge the unverified send-before budget")
    parser.add_argument("--runs", type=int, choices=(1, 2), default=2)
    parser.add_argument("--feedback-code", type=Path)
    parser.add_argument("--feedback-text", type=Path)
    parser.add_argument("--prompt-version", choices=("s01-bootstrap-vl-v1", "s01-bootstrap-vl-v2"),
                        default="s01-bootstrap-vl-v1")
    args = parser.parse_args()
    if bool(args.feedback_code) != bool(args.feedback_text):
        parser.error("Supply both feedback-code and feedback-text")
    cfg = json.loads((ROOT / "config/inference.development.example.json").read_text(encoding="utf-8"))
    case = ROOT / "eval/cases/gap-threshold-v1"
    request = {"operation": "create", "instruction": (case / "instruction.txt").read_text(encoding="utf-8"),
               "currentCode": None, "history": [],
               "context": {"schemaVersion": 1, "projectId": "synthetic-gap", "scriptId": "2",
                           "snapshotRevision": "fixture-v1", "targetFramework": ".NETFramework,Version=v4.7.2",
                           "csharpLanguageVersion": None, "sdkContractVersion": None,
                           "sdkReferenceText": (case / "materials.txt").read_text(encoding="utf-8"),
                           "flowContextText": "模拟工程目录与附图均为同一固定测试版本。"}}
    if args.feedback_code:
        request["operation"] = "modify"
        request["currentCode"] = args.feedback_code.read_text(encoding="utf-8-sig")
        request["instruction"] += "\n本轮反馈（来自本机检查）：\n" + args.feedback_text.read_text(encoding="utf-8-sig")
    prepared = prepare_prompt(request, ROOT / f"config/prompts/{args.prompt_version}.txt",
                              ((case / "flow.png").read_bytes(),), cfg["ImagePolicy"])
    report = ROOT / ".local/reports" / ("gap-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    report.mkdir(parents=True)
    write_json(report / "request-materials.json", request)
    write_json(report / "profile.json", cfg)
    write_json(report / "prepared-manifest.json", {
        "sha256": prepared.content_sha256, "promptVersion": prepared.prompt_version,
        "imageSha256": prepared.image_sha256, "textUtf8Bytes": prepared.text_utf8_bytes,
        "budget": asdict(check_budget(prepared, cfg)),
        "python": sys.version, "packages": {name: importlib.metadata.version(name) for name in ("httpx", "pillow")}})
    hardware = subprocess.run(["C:/Windows/System32/nvidia-smi.exe",
                              "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10)
    write_json(report / "environment.json", {"utc": datetime.now(timezone.utc).isoformat(),
               "os": platform.platform(), "machine": platform.machine(), "gpu": hardware.stdout.strip()})
    adapter = OllamaAdapter(cfg)
    try:
        version = (await adapter.client.get("/api/version")).json()
        tags = (await adapter.client.get("/api/tags")).json()
        identity = next(item for item in tags["models"] if item["name"] == cfg["Model"])
        lock = json.loads((ROOT / cfg["ModelLockPath"]).read_text(encoding="utf-8"))
        show = (await adapter.client.post("/api/show", json={"model": cfg["Model"]})).json()
        template_hash = hashlib.sha256(show.get("template", "").encode()).hexdigest()
        if (version["version"] != cfg["EngineVersion"] or identity["digest"] != lock["model"]["digest"]
                or template_hash != lock["template_sha256"]):
            raise RuntimeError("Profile identity changed")
        write_json(report / "identity.json", {"version": version, "model": identity, "templateSha256": template_hash})
        before = (await adapter.client.get("/api/ps")).json()
        write_json(report / "ps-before.json", before)
        if before.get("models") and not args.feedback_code:
            raise RuntimeError("Unload the model before measuring a cold first call")
        print(f"report={report}", flush=True)
        for number in range(1, args.runs + 1):
            stop, samples = threading.Event(), []
            worker = threading.Thread(target=sample_gpu, args=(stop, samples), daemon=True)
            worker.start()
            start = time.monotonic()
            try:
                generation = await adapter.chat(prepared)
                wall = time.monotonic() - start
            except BaseException as error:
                write_json(report / f"run-{number}-error.json", {"type": type(error).__name__, "error": str(error),
                                                               "elapsedSeconds": time.monotonic() - start})
                raise
            finally:
                stop.set()
                worker.join(timeout=12)
                write_json(report / f"run-{number}-gpu.json", samples)
            write_json(report / f"run-{number}-response.json", generation.raw)
            (report / f"run-{number}-output.txt").write_text(generation.text, encoding="utf-8")
            ps = (await adapter.client.get("/api/ps")).json()
            write_json(report / f"run-{number}-ps.json", ps)
            metrics = generation.metrics
            summary = {"run": number, "wallSeconds": wall, "complete": generation.is_complete,
                       "finishReason": generation.finish_reason, **metrics,
                       "outputChars": len(generation.text), "outputUtf8Bytes": len(generation.text.encode()),
                       "budgetStatus": "unverified; observed counters only"}
            write_json(report / f"run-{number}-summary.json", summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        await adapter.aclose()


if __name__ == "__main__":
    asyncio.run(main())

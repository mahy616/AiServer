"""Target-host Strata smoke check. Stop AiServer first; no account/key/database changes."""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
from aiserver.settings import Settings
from aiserver.operations.lock import InstanceLock
from aiserver.inference.strata import load_strata_profile, StrataBudget, StrataProvider
from aiserver.inference.contracts import TaskInput


def resources():
    result = {}
    try:
        import psutil
        result["ram_available_bytes"] = psutil.virtual_memory().available
    except ImportError:
        pass
    try:
        value = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if value.returncode == 0:
            result["gpu_mib_mib_percent"] = value.stdout.strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


async def verify(settings, long_context, destination):
    cfg, identity, profile = load_strata_profile(settings)
    provider, budget = StrataProvider(settings, cfg, identity), StrataBudget(cfg)
    report = {"model": cfg["Model"], "context": cfg["NumCtx"], "checks": [], "passed": False,
              "scope": "load/protocol/stop-recovery; no business-quality or full-16K-output acceptance", "before": resources()}
    try:
        started = time.monotonic()
        await provider.start()
        report["load_seconds"] = time.monotonic() - started
        for thinking in (False, True):
            text = "Reply with OK."
            if long_context:
                text = "x " * int((cfg["NumCtx"] - 256 - cfg["ReservedTokens"] - 512) * .95) + "\nReply with OK."
            task = TaskInput(json.dumps({"protocol": "openai-chat-v1", "messages": [{"role": "user", "content": [
                {"type": "text", "text": text}]}], "think": thinking, "options": {"num_predict": 256 if long_context else 64}}), ())
            check = await budget.prepare(task, profile)
            if check.code:
                raise ValueError(check.code)
            counts = {"content": 0, "reasoning_content": 0}
            async def delta(value, channel="content"):
                counts[channel] += len(value)
            started = time.monotonic()
            output = await provider.generate(check.prepared, asyncio.Event(), delta)
            row = {"thinking": thinking, "elapsed_seconds": time.monotonic()-started, "characters": counts,
                "prompt_tokens": output.input_tokens, "output_tokens": output.output_tokens,
                "finish_reason": output.finish_reason, "complete": output.is_complete, "resources": resources()}
            row["passed"] = sum(counts.values()) > 0 and (not long_context or
                type(output.input_tokens) is int and .8*cfg["NumCtx"] <= output.input_tokens < cfg["NumCtx"])
            report["checks"].append(row)
            if not row["passed"]:
                raise ValueError("Smoke check did not meet its declared scope")
        # Cancel an owned live generation, then require real idle/owned restart before another request.
        task = TaskInput(json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": "Count upwards for a long time."}]}],
            "options": {"num_predict": 4096}, "think": True}), ())
        check = await budget.prepare(task, profile)
        first = asyncio.Event()
        async def on_first(text, channel="content"):
            first.set()
        running = asyncio.create_task(provider.generate(check.prepared, asyncio.Event(), on_first))
        try:
            await asyncio.wait_for(first.wait(), 120)
        finally:
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass
        report["stop_recovered"] = await provider.recover()
        if not report["stop_recovered"]:
            raise ValueError("GPU cleanup/recovery was not confirmed")
        check = await budget.prepare(TaskInput(json.dumps({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Reply with OK."}]}], "options": {"num_predict": 32}, "think": False}), ()), profile)
        output = await provider.generate(check.prepared, asyncio.Event(), on_first)
        report["generation_after_stop"] = bool(output.text)
        if not report["generation_after_stop"]:
            raise ValueError("No response after stop recovery")
        report["passed"] = True
    except Exception as error:
        report["error"] = type(error).__name__ + ": " + str(error)
    finally:
        await provider.aclose()
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", default=str(ROOT / "config/server.strata.local.json"))
    parser.add_argument("--long-context", action="store_true", help="Also test a near-window synthetic prompt; can take several minutes")
    args = parser.parse_args()
    settings = Settings.load(args.server_config)
    if settings.provider != "Strata":
        parser.error("Select a Strata service configuration")
    output = ROOT / ".local/strata/validation" / (uuid4().hex + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with InstanceLock(settings.data_directory):
        passed = asyncio.run(verify(settings, args.long_context, output))
    print(("PASS" if passed else "FAIL") + ": " + str(output))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

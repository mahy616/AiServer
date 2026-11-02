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


async def tool_roundtrip(provider, budget, profile):
    messages = [{"role": "user", "content": [{"type": "text", "text":
        "Use read_sample to read sample.txt, then repeat its returned marker exactly."}]}]
    tools = [{"type": "function", "function": {"name": "read_sample", "description": "Read the requested sample file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
    async def ignore(text, channel="content"):
        pass
    async def generate(choice):
        request = {"protocol": "openai-chat-v1", "messages": messages, "tools": tools, "think": False,
            "tool_choice": choice, "parallel_tool_calls": False, "options": {"num_predict": min(512, budget.cfg["NumPredict"])}}
        check = await budget.prepare(TaskInput(json.dumps(request), ()), profile)
        if check.code:
            raise ValueError(check.code)
        return await provider.generate(check.prepared, asyncio.Event(), ignore)
    first = await generate("required")
    if not first.is_complete or len(first.tool_calls) != 1:
        raise ValueError("Model did not return one complete native tool call")
    call = first.tool_calls[0]
    if call["function"]["name"] != "read_sample" or json.loads(call["function"]["arguments"]) != {"path": "sample.txt"}:
        raise ValueError("Model returned incorrect sample tool arguments")
    messages.extend([{"role": "assistant", "content": [{"type": "text", "text": first.text}],
        "reasoning_content": first.reasoning_text, "tool_calls": [{**call, "function": {
            **call["function"], "arguments": json.loads(call["function"]["arguments"])}}]},
        {"role": "tool", "tool_call_id": call["id"], "content": [{"type": "text", "text": "STRATA_TOOL_OK_42"}]}])
    second = await generate("none")
    if not second.is_complete or second.tool_calls or "STRATA_TOOL_OK_42" not in second.text:
        raise ValueError("Model did not consume the associated tool result")
    return {"passed": True, "requests": 2, "scope": "synthetic tool result; no client files read or written"}


async def verify(settings, long_context, destination, *, benchmark=False):
    cfg, identity, profile = load_strata_profile(settings)
    provider, budget = StrataProvider(settings, cfg, identity), StrataBudget(cfg)
    report = {"model": cfg["Model"], "context": cfg["NumCtx"], "max_output": cfg["NumPredict"],
              "active_experts": cfg.get("ActiveExperts"), "draft_mode_configured": cfg.get("DraftMode", "MTP"),
              "checks": [], "passed": False,
              "scope": "load/protocol/tools/stop-recovery; no business-quality or full-16K-output acceptance", "before": resources()}
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
            first_delta = None
            async def delta(value, channel="content"):
                nonlocal first_delta
                if first_delta is None:
                    first_delta = time.monotonic()
                counts[channel] += len(value)
            started = time.monotonic()
            output = await provider.generate(check.prepared, asyncio.Event(), delta)
            row = {"thinking": thinking, "elapsed_seconds": time.monotonic()-started, "characters": counts,
                "first_delta_seconds": first_delta-started if first_delta is not None else None,
                "prompt_tokens": output.input_tokens, "output_tokens": output.output_tokens,
                "finish_reason": output.finish_reason, "complete": output.is_complete, "resources": resources()}
            row["passed"] = sum(counts.values()) > 0 and (not long_context or
                type(output.input_tokens) is int and .8*cfg["NumCtx"] <= output.input_tokens < cfg["NumCtx"])
            report["checks"].append(row)
            if not row["passed"]:
                raise ValueError("Smoke check did not meet its declared scope")
        report["tools"] = await tool_roundtrip(provider, budget, profile)
        if benchmark:
            task = TaskInput(json.dumps({"protocol": "openai-chat-v1", "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Write only a C# static method named Mean(double[] values) that returns the mean, checks null and throws ArgumentException for an empty input. Do not use LINQ."}]}],
                "think": False, "options": {"num_predict": min(512, cfg["NumPredict"])}}), ())
            check = await budget.prepare(task, profile)
            if check.code:
                raise ValueError(check.code)
            first_delta = None
            async def code_delta(value, channel="content"):
                nonlocal first_delta
                if first_delta is None:
                    first_delta = time.monotonic()
            started = time.monotonic()
            output = await provider.generate(check.prepared, asyncio.Event(), code_delta)
            elapsed = time.monotonic()-started
            report["code_sample"] = {"elapsed_seconds": elapsed, "first_delta_seconds": first_delta-started if first_delta else None,
                "input_tokens": output.input_tokens, "output_tokens": output.output_tokens,
                "end_to_end_tokens_per_second": output.output_tokens/elapsed if type(output.output_tokens) is int else None,
                "complete": output.is_complete, "finish_reason": output.finish_reason,
                "nonempty": bool(output.text), "resources": resources(), "compiled_or_quality_checked": False}
            if not output.is_complete or not output.text:
                raise ValueError("Code generation sample was empty or truncated")
        # Cancel an owned live generation, then require real idle/owned restart before another request.
        task = TaskInput(json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": "Count upwards for a long time."}]}],
            "options": {"num_predict": min(4096, cfg["NumPredict"])}, "think": True}), ())
        check = await budget.prepare(task, profile)
        if check.code:
            raise ValueError(check.code)
        first = asyncio.Event()
        async def on_first(text, channel="content"):
            first.set()
        running = asyncio.create_task(provider.generate(check.prepared, asyncio.Event(), on_first))
        try:
            await asyncio.wait_for(first.wait(), min(600, cfg["GenerateTimeoutSeconds"]))
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


def run_one(config_path, long_context, output, *, benchmark=False):
    try:
        settings = Settings.load(str(config_path))
        if settings.provider != "Strata":
            raise ValueError("Select a Strata service configuration")
        with InstanceLock(settings.data_directory):
            if benchmark:
                return asyncio.run(verify(settings, long_context, output, benchmark=True))
            return asyncio.run(verify(settings, long_context, output))
    except Exception as error:
        output.write_text(json.dumps({"passed": False, "config": str(config_path),
            "error": type(error).__name__ + ": " + str(error)}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--server-config")
    choice.add_argument("--all", action="store_true", help="Test Qwen and DeepSeek sequentially; both must be prepared and AiServer stopped")
    parser.add_argument("--long-context", action="store_true", help="Also test a near-window synthetic prompt; can take several minutes")
    parser.add_argument("--benchmark", action="store_true", help="Compare a fixed C# generation sample; does not compile or judge business quality")
    parser.add_argument("--pause", action="store_true")
    args = parser.parse_args()
    try:
        directory = ROOT / ".local/strata/validation" / uuid4().hex
        directory.mkdir(parents=True)
        selected = [("qwen", ROOT / "config/server.strata.local.json"), ("deepseek", ROOT / "config/server.strata.deepseek.local.json")] if args.all else [
            ("selected", Path(args.server_config) if args.server_config else ROOT / "config/server.strata.local.json")]
        results = {}
        for name, config_path in selected:
            output = directory / (name + ".json")
            print("Testing " + name + ": " + str(config_path), flush=True)
            try:
                passed = run_one(config_path, args.long_context, output, benchmark=args.benchmark)
                result = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {"passed": passed}
                result["passed"] = passed
            except Exception as error:
                result = {"passed": False, "error": type(error).__name__ + ": " + str(error)}
                if not args.all:
                    raise
            results[name] = result
            print(("PASS" if result["passed"] else "FAIL") + ": " + str(output), flush=True)
        summary = {"passed": all(v["passed"] for v in results.values()), "models": results,
            "resource_scope": "host snapshots, not measured peaks", "business_quality_accepted": False}
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Summary: " + str(directory / "summary.json"), flush=True)
        return 0 if summary["passed"] else 1
    finally:
        if args.pause:
            input("Press Enter to close...")


if __name__ == "__main__":
    raise SystemExit(main())

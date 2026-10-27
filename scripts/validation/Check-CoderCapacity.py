"""Stopped-service GPU/context check with synthetic code; not a coding benchmark."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
sys.path.insert(0, str(ROOT / "scripts/inference"))
spec = importlib.util.spec_from_file_location("capacity_installer", ROOT / "scripts/inference/Install-Qwen25Coder7B.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def prepared(cfg, text, **extra):
    from aiserver.context.prompt_builder import prepare_messages
    return prepare_messages({"protocol": "openai-chat-v1", "messages": [
        {"role": "user", "content": [{"type": "text", "text": text}]}], **extra}, (), cfg["ImagePolicy"])


async def target_prompt(cfg, counter, target):
    """Fit near the requested token count using the engine's rendered template."""
    tag = uuid4().hex[:12]
    expected = {key: uuid4().hex[:12] for key in ("CapacityStart", "CapacityMiddle", "CapacityEnd")}
    rows = [f'// item {i:05d}: code {((i + 1) * 2654435761) & 0xffffffff:08x}; preserve existing behavior.\n'
            for i in range(target // 4 + 128)]
    def prompt(n, padding=0):
        mid = n // 2
        text = (f"Capacity check {tag}. Read the C# inventory below.\n"
                f'const string CapacityStart = "{expected["CapacityStart"]}";\n' + "".join(rows[:mid]) +
                f'const string CapacityMiddle = "{expected["CapacityMiddle"]}";\n' + "".join(rows[mid:n]) +
                f'const string CapacityEnd = "{expected["CapacityEnd"]}";\n' + "//" + " x" * padding +
                "\nReturn only a JSON object containing the exact values of CapacityStart, CapacityMiddle, CapacityEnd.")
        return prepared(cfg, text)
    low, high = 0, len(rows)
    best, count = prompt(0), await counter.count(prompt(0))
    if count > target:
        raise ValueError("Requested input is smaller than the check instructions")
    while low < high:
        n = (low + high + 1) // 2
        candidate = prompt(n)
        current = await counter.count(candidate)
        if current <= target:
            low, best, count = n, candidate, current
        else:
            high = n - 1
    # A short comment pads the remaining gap without changing the question.
    candidate = prompt(low, max(0, target - count))
    current = await counter.count(candidate)
    if count <= current <= target:
        best, count = candidate, current
    if not target - 64 <= count <= target:
        raise ValueError("Could not construct an input within 64 tokens of the target")
    return best, count, expected


def retrieved(text, expected):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text) == expected
    except ValueError:
        return False


async def exercise(cfg, lock, work, inputs, report):
    from aiserver.context.coder_budget import CoderCounter
    from aiserver.inference.runtime import RealProvider
    counter = CoderCounter(cfg, ROOT)
    provider = RealProvider(SimpleNamespace(manage_engine=False, data_directory=work), cfg, lock)
    try:
        await provider.start()
        report["allocation"] = await provider.adapter.ensure_full_gpu()
        print("完整窗口已加载并通过全GPU驻留检查。", flush=True)
        short = await provider.adapter.chat(prepared(cfg, "Reply with exactly OK.", options={"num_predict": 32}))
        report["short_reply"] = short.text
        for target in inputs:
            print(f"正在构造并处理接近{target} tokens的合成代码输入……", flush=True)
            prompt, admitted, expected = await target_prompt(cfg, counter, target)
            prompt = replace(prompt, options_json=json.dumps({"num_predict": 160}))
            start, first = time.perf_counter(), None
            async def delta(text):
                nonlocal first
                if first is None:
                    first = time.perf_counter() - start
            result = await provider.adapter.chat(prompt, delta)
            elapsed = time.perf_counter() - start
            actual = result.metrics.get("prompt_eval_count")
            row = {"target_input_tokens": target, "admission_tokens": admitted,
                   "actual_input_tokens": actual, "token_count_matches": actual == admitted,
                   "first_token_seconds": first, "wall_seconds": elapsed,
                   "metrics": result.metrics, "retrieval_passed": retrieved(result.text, expected),
                   "answer": result.text, "allocation": await provider.adapter.ensure_full_gpu()}
            report["checks"].append(row)
            print(json.dumps({k: row[k] for k in ("actual_input_tokens", "first_token_seconds", "wall_seconds",
                                                 "token_count_matches", "retrieval_passed")}, ensure_ascii=False), flush=True)
            if actual != admitted:
                raise ValueError("实际输入计数不符，不能确认完整输入验收通过")
        report["passed"] = all(row["token_count_matches"] and row["retrieval_passed"] for row in report["checks"])
    finally:
        await provider.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--inference", type=Path, default=Path("config/inference.coder14b.local.json"))
    parser.add_argument("--inputs", type=int, nargs="+", default=[32768, 49152, 65536])
    args = parser.parse_args()
    cfg = installer.read(installer.support.resolve(ROOT, args.inference))
    from aiserver.context.window_budget import input_budget
    if any(size < 512 or size > input_budget(cfg, min(160, cfg["NumPredict"])) for size in args.inputs):
        parser.error("inputs must fit the configured window minus the check's output budget and reserve")
    # Capacity checks always require all-GPU, including when comparing a 7B profile.
    cfg["RequireFullGpu"] = True
    lock = installer.read(installer.support.resolve(ROOT, cfg["ModelLockPath"]))
    server = installer.read(installer.legacy.select_config(ROOT, args.server_config))
    work = ROOT / ".local/coder14b/checks" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    work.mkdir(parents=True)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "model": cfg["Model"],
              "context": cfg["NumCtx"], "kv": cfg["KvCacheType"], "batch": cfg["NumBatch"],
              "kind": "synthetic-context-and-gpu-check-not-coding-benchmark", "checks": [], "passed": False}
    try:
        with installer.legacy.offline(ROOT, server):
            with installer.legacy.temporary_engine(ROOT, cfg, work) as fetch:
                cfg["BaseUrl"] = fetch.base_url
                asyncio.run(exercise(cfg, lock, work, args.inputs, report))
    except KeyboardInterrupt:
        report["error"] = "Cancelled"
    except Exception as error:
        # No user documents or private configuration are included in this report.
        report["error"] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
    path = work / "summary.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("合成上下文检查" + ("通过" if report["passed"] else "未通过") + "；不代表真实代码能力验收。", flush=True)
    if report.get("error"):
        print(report["error"], flush=True)
    print("报告及本次engine.log：" + str(work), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

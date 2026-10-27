"""Stopped-service comparison through a temporary real AiServer API; no public keys."""
import argparse
import ast
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
sys.path.insert(0, str(ROOT / "scripts/inference"))
spec = importlib.util.spec_from_file_location("comparison_shared", ROOT / "scripts/inference/Install-Qwen3VL8B.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

CONFIGS = {"7b": "config/inference.coder.local.json", "14b": "config/inference.coder14b.local.json",
           "qwen35": "config/inference.qwen35.local.json"}
CODE_PROMPT = '''Return only Python code defining merge_intervals(intervals).
Input is a list of (start, end) integer tuples, with start <= end. Return a sorted
list of tuples merging overlapping or touching intervals. Do not mutate the input.
Handle empty input, negative endpoints, duplicates, and contained intervals.
Use only this function, standard arithmetic, comparisons, loops and lists/tuples.
Available builtins: sorted, list, tuple, len, range, min, max, int, bool, enumerate,
zip, reversed. Allowed methods: append, extend, sort. No imports or other helpers.'''


def grade_code(text):
    """Run only a small AST-restricted function, in a separate timed process."""
    text = re.sub(r"^```(?:python)?\s*|\s*```$", "", text.strip())
    tree = ast.parse(text)
    allowed = {"Module", "FunctionDef", "arguments", "arg", "Return", "Assign", "AnnAssign", "AugAssign", "Expr",
        "If", "IfExp", "For", "While", "Break", "Continue", "Pass", "Name", "Load", "Store", "Constant", "List", "Tuple",
        "Subscript", "Slice", "Attribute", "Call", "keyword", "Compare", "BoolOp", "UnaryOp", "BinOp", "And", "Or",
        "Not", "USub", "UAdd", "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE",
        "Is", "IsNot", "In", "NotIn", "ListComp", "comprehension"}
    builtins = {key: getattr(__import__("builtins"), key) for key in
                ("sorted", "list", "tuple", "len", "range", "min", "max", "int", "bool", "enumerate", "zip", "reversed")}
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != "merge_intervals":
        raise ValueError("Expected one merge_intervals function")
    for node in ast.walk(tree):
        if type(node).__name__ not in allowed or (isinstance(node, ast.Name) and node.id.startswith("_")):
            raise ValueError("Code outside the permitted evaluation subset")
        if isinstance(node, ast.Attribute) and node.attr not in ("append", "extend", "sort"):
            raise ValueError("Unsupported attribute")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in builtins or
                                               isinstance(node.func, ast.Attribute)):
            raise ValueError("Unsupported call")
        if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.returns or node.args.defaults or node.args.kw_defaults):
            raise ValueError("Unsupported function signature")
    namespace = {"__builtins__": builtins}
    exec(compile(tree, "<model-evaluation>", "exec"), namespace)
    cases = [([], []), ([(5, 7), (1, 3), (3, 5)], [(1, 7)]),
             ([(-5, -1), (-4, -3), (2, 2), (2, 2), (4, 9)], [(-5, -1), (2, 2), (4, 9)]),
             ([(1, 10), (2, 3), (20, 22), (21, 30)], [(1, 10), (20, 30)])]
    passed = 0
    for values, expected in cases:
        original = list(values)
        actual = namespace["merge_intervals"](values)
        passed += actual == expected and values == original
    return {"passed": passed == len(cases), "passed_cases": passed, "total_cases": len(cases)}


def evaluate_code(text):
    env = {key: value for key, value in os.environ.items() if key.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
    try:
        result = subprocess.run([sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--grade-code"],
            input=json.dumps(text), text=True, encoding="utf-8", capture_output=True, timeout=5, env=env)
        return json.loads(result.stdout) if result.returncode == 0 else {"passed": False, "error": "restricted-code-evaluation-failed"}
    except subprocess.TimeoutExpired:
        return {"passed": False, "error": "code-evaluation-timeout"}


def run_profile(cfg, path, work, marker, padding_lines, *, think=False):
    from fastapi.testclient import TestClient
    from aiserver.app import create_app
    from aiserver.settings import Settings
    from aiserver.inference.gpu import require_gpu_status
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        cfg = {**cfg, "BaseUrl": f"http://127.0.0.1:{probe.getsockname()[1]}", "RequireFullGpu": True}
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    settings = Settings(provider="Ollama", environment="Development", manage_natapp=False, inference_config=path,
                        data_directory=work / "data", execution_timeout_seconds=3600,
                        requests_per_minute=1000, submissions_per_minute=1000)
    app = create_app(settings)
    store = app.state.store
    store.initialize()
    store.user_add("comparison", "Temporary comparison")
    token = store.token_create("comparison")
    row = {"model": cfg["Model"], "context": cfg["NumCtx"], "batch": cfg["NumBatch"],
           "kv": cfg.get("KvCacheType", "f16"), "thinking": think,
           "sampling": cfg.get("GenerationDefaults", {}),
           "capacity_method": cfg["Budget"]["Method"], "checks": [], "passed": False}
    started = time.perf_counter()
    try:
        with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
            row["startup_seconds"] = time.perf_counter() - started
            import requests
            with requests.Session() as local:
                local.trust_env = False
                response = local.get(cfg["BaseUrl"] + "/api/ps", timeout=10)
                response.raise_for_status()
                row["allocation"] = require_gpu_status(cfg, response.json())
            def chat(name, messages, **options):
                start = time.perf_counter()
                response = client.post("/v1/chat/completions", json={"model": cfg["Model"], "messages": messages, "think":think,
                    "max_tokens": cfg["NumPredict"], **options})
                entry = {"name": name, "wall_seconds": time.perf_counter() - start, "passed": False,
                         "http_status": response.status_code}
                row["checks"].append(entry)
                if response.status_code != 200:
                    entry["error"] = response.json().get("error", {}).get("code", "request-failed")
                    return None, entry
                value = response.json()
                entry.update(usage=value.get("usage"), finish_reason=value["choices"][0]["finish_reason"],
                             job_id=value["id"].removeprefix("chatcmpl-"))
                return value["choices"][0]["message"], entry
            message, check = chat("text", [{"role": "user", "content": "Reply with exactly OK."}])
            if message:
                check.update(answer=message.get("content"), passed=(message.get("content") or "").strip() == "OK")
            start = time.perf_counter()
            with client.stream("POST", "/v1/chat/completions", json={"model": cfg["Model"], "stream": True, "think":think,
                    "messages": [{"role": "user", "content": "Reply with exactly STREAM_OK."}],
                    "max_tokens": cfg["NumPredict"]}) as response:
                text, done, job_id = "", False, None
                for line in response.iter_lines():
                    if line == "data: [DONE]":
                        done = True
                    elif line.startswith("data: "):
                        event = json.loads(line[6:])
                        job_id = event.get("id", "").removeprefix("chatcmpl-") or job_id
                        delta = next(iter(event.get("choices", [])), {}).get("delta", {}).get("content", "")
                        text += delta
                row["checks"].append({"name": "stream", "passed": response.status_code == 200 and done and text.strip() == "STREAM_OK",
                    "answer": text, "wall_seconds": time.perf_counter() - start,
                    "http_status": response.status_code, "job_id": job_id})
            tool = {"type": "function", "function": {"name": "read_file", "description": "Read a text file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
            messages = [{"role": "user", "content": "Call read_file for compare.txt, then return exactly its contents."}]
            assistant, check = chat("tool_call", messages, tools=[tool], tool_choice="required")
            if assistant:
                calls = assistant.get("tool_calls", [])
                check["passed"] = len(calls) == 1 and calls[0]["function"]["name"] == "read_file" and json.loads(calls[0]["function"]["arguments"]) == {"path": "compare.txt"}
                if check["passed"]:
                    messages += [assistant, {"role": "tool", "tool_call_id": calls[0]["id"], "content": marker}]
                    message, check = chat("tool_result", messages)
                    if message:
                        check.update(answer=message.get("content"), passed=(message.get("content") or "").strip() == marker)
            message, check = chat("coding", [{"role": "user", "content": CODE_PROMPT}])
            if message:
                check.update(answer=message.get("content"), **evaluate_code(message.get("content") or ""))
            if padding_lines:
                material = "".join(f"// inventory line {n:06d}: preserve existing behavior and references.\n" for n in range(padding_lines))
                prompt = "StartMarker=" + marker + "\n" + material + "\nReturn only the exact value of StartMarker."
                message, check = chat("long_input", [{"role": "user", "content": prompt}])
                if message:
                    check.update(answer=message.get("content"), passed=(message.get("content") or "").strip() == marker)
            with store.connection() as connection:
                payloads = connection.execute("SELECT effective_settings_json,metrics_json FROM job_payloads").fetchall()
                performance = {item["job_id"]: dict(item) for item in connection.execute("SELECT * FROM task_performance")}
            from aiserver.operations.monitoring import performance_row
            for check in row["checks"]:
                item = performance.get(check.get("job_id"))
                if item:
                    measured = performance_row(item)
                    check["performance"] = {key: measured[key] for key in ("first_output_ms", "total_ms", "load_ms",
                        "prompt_eval_ms", "eval_ms", "input_tokens", "output_tokens", "tokens_per_second")}
            row["token_counts"] = [{"admission_estimate": json.loads(item[0])["prepared"]["estimated_input_tokens"],
                                   "actual": json.loads(item[1])["inputTokens"]} for item in payloads if item[1]]
            row["passed"] = len(row["checks"]) >= 5 and all(check["passed"] for check in row["checks"])
    except Exception as error:
        row["error"] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
    return row


def main():
    if sys.argv[1:] == ["--grade-code"]:
        try:
            print(json.dumps(grade_code(json.loads(sys.stdin.read()))))
            return 0
        except Exception:
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--models", nargs="+", choices=tuple(CONFIGS), help="Default: every prepared candidate")
    parser.add_argument("--thinking", choices=("off", "on", "both"), default="both", help="Qwen3.5 only")
    parser.add_argument("--padding-lines", type=int, default=0, help="Optional identical long material for every model")
    args = parser.parse_args()
    if not 0 <= args.padding_lines <= 12000:
        parser.error("padding-lines must be between 0 and 12000")
    candidates = args.models or [name for name, path in CONFIGS.items() if (ROOT / path).is_file()]
    if not candidates:
        parser.error("No prepared models; run an installation entry first")
    server = legacy.read(legacy.select_config(ROOT, args.server_config))
    work = ROOT / ".local/model-comparisons" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    work.mkdir(parents=True)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "kind": "small-real-api-comparison-not-business-acceptance",
              "padding_lines": args.padding_lines, "models": [], "passed": False,
              "timing_note": "first_output_ms is measured inside AiServer and includes preparation/queue; not public-network TTFT"}
    marker = "TOOL_" + uuid4().hex
    try:
        with legacy.offline(ROOT, server):
            for name in candidates:
                cfg = legacy.read(ROOT / CONFIGS[name])
                modes = ([False, True] if args.thinking == "both" else [args.thinking == "on"]) if name == "qwen35" else [False]
                for think in modes:
                    current = {**cfg, **({"GenerationDefaults": {"presence_penalty": 0.0,
                        "temperature": 0.6 if think else 0.7, "top_p": 0.95 if think else 0.8}} if name == "qwen35" else {})}
                    directory = work / (name + ("-thinking" if think else "-instruct"))
                    directory.mkdir()
                    print("检查 " + current["Model"] + f"，thinking={think}", flush=True)
                    try:
                        result = run_profile(current, directory / "inference.json", directory, marker, args.padding_lines, think=think)
                    except Exception as error:
                        result = {"model": current["Model"], "thinking": think, "passed": False, "error": type(error).__name__}
                    report["models"].append(result)
                    (work / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report["passed"] = all(row["passed"] for row in report["models"])
    except KeyboardInterrupt:
        report["error"] = "Cancelled"
    except Exception as error:
        report["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
    (work / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("对比检查" + ("通过" if report["passed"] else "存在失败项") + "；请查看每项成绩和耗时，不代表业务代码验收。")
    print("报告：" + str(work / "summary.json"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

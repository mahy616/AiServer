"""真实OpenCode文件工具与同会话normal→thinking→normal检查；只操作独立临时文件。"""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts/client"), str(Path(__file__).parent)]
from opencode_support import BINARY, saved_key, probe
from csharp_benchmark.common import MODELS, utc, write_json
from csharp_benchmark.relay import Recorder, wait_ready

spec = importlib.util.spec_from_file_location("tools_benchmark_client", Path(__file__).with_name("Benchmark-CSharp.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def verify(directory, binary, info, record, *, thinking="both", normal_timeout=180, thinking_timeout=600):
    directory = Path(directory)
    (directory / "workspace").mkdir(parents=True, exist_ok=True)
    modes = [False, True, False] if thinking == "both" and info.get("thinking") else [thinking == "on"]
    if modes == [True] and not info.get("thinking"):
        raise ValueError("所选模型不支持thinking")
    if not info.get("toolCall"):
        raise ValueError("所选模型不支持文件工具")
    results, session = [], None
    with Recorder(record["server"], record["apiKey"], directory / "http", info["id"], expected_thinking=modes[0]) as relay:
        env, config, config_path = runner.client_environment(directory, {**info, "benchmark_thinking": modes[0]}, relay)
        config["permission"].update(write="allow", edit="allow", apply_patch="allow")
        write_json(config_path, config)
        for index, think in enumerate(modes):
            name = f"{index + 1}-" + ("thinking" if think else "normal")
            proof = "FILE_TOOL_PROOF_" + uuid4().hex
            source = directory / "workspace" / (name + "-note.txt")
            target = directory / "workspace" / (name + "-copy.txt")
            source.write_text(proof + "\n", encoding="utf-8")
            prompt = (f"使用真实read工具读取文件{source}。工具返回结果中，只取<content>标签内的文件正文，"
                      f"去掉行号、路径、类型、End of file标记和任何工具说明，再使用真实文件写入工具把这一行原文保存到{target}。"
                      "写入内容必须只包含文件正文，不要附加解释或元数据；不要猜测内容，不运行命令、不委派、不访问其他文件。完成后简短说明。")
            timeout = thinking_timeout if think else normal_timeout
            relay.expected_thinking, relay.phase, relay.deadline = think, name, time.monotonic() + timeout
            print(directory.name + ": " + name, flush=True)
            result = runner.phase(binary, directory, env, info["id"], prompt, name, timeout, session,
                                  stop_event=relay.terminal_error, variant=("thinking" if think else "normal") if info.get("thinking") else None)
            previous = session
            session = result.get("session_id") or session
            tools = result.get("tools", [])
            read = any(t["name"] == "read" and t["file"] == source.name and t["status"] == "completed" for t in tools)
            wrote = any(t["name"] in ("write", "edit", "apply_patch") and t["status"] == "completed" for t in tools)
            exact = target.is_file() and target.read_text(encoding="utf-8-sig").strip() == proof
            requests = [r for r in relay.rows if r["phase"] == name]
            passed = bool(read and wrote and exact and result.get("finished") and not result.get("timed_out")
                          and not result.get("aborted") and result.get("exit_code") == 0 and session
                          and result.get("session_count") == 1 and (previous is None or previous == session)
                          and requests and all(r.get("done") and not r.get("error") and not r.get("transport_error")
                              and r.get("requested_thinking") is think for r in requests))
            results.append({"phase": name, "thinking": think, "passed": passed, "read_completed": read,
                            "write_completed": wrote, "exact_content": exact, "session_id": session,
                            "seconds": result.get("seconds"), "timeout_seconds": timeout,
                            "reasoning_chars": sum(r.get("reasoning_chars", 0) for r in requests),
                            "request_count": len(requests), "timed_out": result.get("timed_out"), "tools": tools})
            write_json(directory / "result.json", {"model": info["id"], "passed": all(r["passed"] for r in results), "phases": results})
            if not passed:
                break
    return {"model": info["id"], "passed": len(results) == len(modes) and all(r["passed"] for r in results), "phases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=tuple(MODELS), default=["9b", "qwen38"])
    parser.add_argument("--thinking", choices=("off", "on", "both"), default="both")
    parser.add_argument("--normal-timeout", type=int, default=180)
    parser.add_argument("--thinking-timeout", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.normal_timeout <= 86400 or not 1 <= args.thinking_timeout <= 86400:
        parser.error("限时须为1到86400秒")
    work = (args.output or ROOT / ".local/model-tools" / uuid4().hex).resolve()
    work.mkdir(parents=True, exist_ok=False)
    summary = {"started_at": utc(), "passed": False, "models": {}}
    try:
        record = saved_key()
        available = {m["id"].lower(): m for m in probe(record)["/api/v1/models"]["models"]}
        summary["opencode_version"] = subprocess.run([str(BINARY), "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        for alias in args.models:
            info = available[MODELS[alias].lower()]
            wait_ready(record["server"], record["apiKey"])
            summary["models"][alias] = verify(work / alias, BINARY, info, record, thinking=args.thinking,
                normal_timeout=args.normal_timeout, thinking_timeout=args.thinking_timeout)
            write_json(work / "summary.json", summary)
        summary["passed"] = all(m["passed"] for m in summary["models"].values())
        return 0 if summary["passed"] else 1
    except Exception as error:
        summary["error"] = type(error).__name__
        return 1
    finally:
        summary["finished_at"] = utc()
        write_json(work / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("证据目录：" + str(work))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

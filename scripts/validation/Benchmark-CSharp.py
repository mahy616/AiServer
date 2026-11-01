"""本机OpenCode单文件C#评测；仅生成客户端Markdown报告，正式服务需已启动。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from csharp_benchmark.cases import analysis_result, make_cases
from csharp_benchmark.common import (ACTIVE_MODELS, DEFAULT_MODELS, MODELS, ROOT, TIMEOUTS, THINKING_TIMEOUTS, ROLE_LEVELS, VERSION,
                                     condition_plan, read_events, run_process, sha, utc, write_json)
from csharp_benchmark.harness import check_sdk, evaluate
from csharp_benchmark.probes import exercise
from csharp_benchmark.relay import Recorder, wait_ready
from csharp_benchmark.report import render

sys.path.insert(0, str(ROOT / "scripts/client"))
from opencode_support import model_limits, thinking_variants


def suite_hash():
    return sha(json.dumps([c.__dict__ for c in make_cases()], sort_keys=True, ensure_ascii=False))


def prepare(output, aliases, levels, kinds, thinking="off", profile="calibration", normal_timeout=None, thinking_timeout=None):
    conditions = condition_plan(aliases, thinking)
    if profile not in ("calibration", "roles"):
        raise ValueError("未知评测计划")
    for value in (normal_timeout, thinking_timeout):
        if value is not None and (type(value) is not int or not 1 <= value <= 86400):
            raise ValueError("整题限时须为1到86400秒的整数")
    output.mkdir(parents=True, exist_ok=False)
    state = {"version": VERSION, "suite_sha256": suite_hash(), "run_id": output.name, "created_at": utc(),
             "status": "prepared", "selected_models": aliases, "thinking_selection": thinking,
             "conditions": conditions, "profile": profile,
             "models": {a: MODELS[c["base_alias"]] for a, c in conditions.items()}, "tasks": [], "probes": {}}
    for alias, condition in conditions.items():
        for case in make_cases():
            selected_levels = levels if levels is not None else ROLE_LEVELS[condition["base_alias"]] if profile == "roles" else ("easy",)
            if case.level not in selected_levels or case.kind not in kinds:
                continue
            ident = alias + "-" + case.id
            task = {"id": ident, "model": alias, "thinking": condition["thinking"],
                    "case": case.id, "cap": case.cap, "status": "pending",
                    "timeout_seconds": (thinking_timeout or THINKING_TIMEOUTS[case.level]) if condition["thinking"] else
                                       (normal_timeout or TIMEOUTS[case.level])}
            workspace = output / "cases" / ident / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "Script.cs").write_text(case.source, encoding="utf-8")
            (workspace / "TASK.md").write_text(
                f"# {case.title}\n\n{case.specification}\n\n"
                "唯一交付文件Script.cs；必须定义public static class Script，"
                "在类内定义入口public static double[] Run(double[] input)。"
                f"全部类型及辅助函数必须在同一个文件内，最多{case.cap}行，不要求最低行数。"
                "使用.NET 8内置类型，无文件、网络、异步、缓存或外部库依赖。输入保证有限数、绝对值不超过1e6、记录完整且不超过5000条。"
                "数值比较采用相对1e-7／绝对1e-8容差。完成后测试器记录首次结果，可能提供一次检查反馈。"
                "不要自行运行编译或测试，不委派其他Agent。\n", encoding="utf-8")
            write_json(output / "cases" / ident / "frozen-task.json",
                       {"case": case.id, "specification": case.specification, "initial_sha256": sha(case.source),
                        "task_sha256": sha((workspace / "TASK.md").read_text(encoding="utf-8"))})
            state["tasks"].append(task)
    save(output, state)
    return state


def fallback_source(state, task):
    """Only a failed normal attempt unlocks an independent thinking retry."""
    if state.get("thinking_selection") != "fallback" or not task["thinking"]:
        return None
    base = state["conditions"][task["model"]]["base_alias"]
    return next((t for t in state["tasks"] if t["model"] == base and t["case"] == task["case"]), None)


def fallback_skip_reason(source):
    if source["status"] == "passed":
        return "普通模式已解决，不运行thinking"
    if source["status"] != "failed":
        return "普通模式未取得完整结果，不自动重新生成"
    if source.get("result", {}).get("failure_category") == "transport-or-service":
        return "普通模式基础链路失败，先排障，不评thinking收益"
    return None


def validate_run(state):
    for alias, model in state["models"].items():
        base = state.get("conditions", {}).get(alias, {}).get("base_alias", alias)
        if base not in MODELS or model.lower() != MODELS[base].lower():
            raise ValueError("旧运行包含已移除模型；请新建运行目录，历史报告可用--report-only查看。")
    if state["version"] != VERSION or state["suite_sha256"] != suite_hash():
        raise ValueError("题库或思考测试版本变化，不能混合续跑；请新建运行目录。")
    expected = condition_plan(state["selected_models"], state["thinking_selection"])
    if (state.get("conditions") != expected or set(state["models"]) != set(expected)
            or any(t["model"] not in expected or t.get("thinking") != expected[t["model"]]["thinking"]
                   or type(t.get("thinking")) is not type(expected[t["model"]]["thinking"])
                   for t in state["tasks"])):
        raise ValueError("运行目录中的模型／思考模式发生变化，不能混合续跑。")


def bind_capabilities(state, available):
    capabilities = {}
    for alias, model in state["models"].items():
        if model.lower() not in available:
            raise ValueError(f"服务未发布{alias}模型，停止预检；不会自动安装或切换配置。")
        info = available[model.lower()]
        if state["conditions"][alias]["thinking"] and info.get("thinking") is not True:
            raise ValueError(f"服务未确认{alias}的thinking能力；请更新并重启5080、检查models，不会改成普通模式计分。")
        value = state["conditions"][alias]["thinking"]
        if isinstance(value, str) and value not in info.get("thinkingLevels", []):
            raise ValueError(f"服务未确认{alias}的思考等级。")
        capabilities[alias] = info
    return capabilities


def save(output, state):
    write_json(output / ".state.json", state)
    render(state, output / "客户端报告.md")


def recover_logs(directory, result):
    """Even interrupted sessions retain their existing request/job correlation."""
    rows = read_events(directory / "http/requests.jsonl")
    latest = {}
    for row in rows:
        latest[row["request_id"]] = row
    result["requests"] = list(latest.values())
    if not result.get("session_id"):
        for name in ("reading", "implementation"):
            for event in read_events(directory / (name + ".jsonl")):
                session = event.get("sessionID") or event.get("part", {}).get("sessionID")
                if session:
                    result["session_id"] = session
                    break
    return result


@contextmanager
def run_lock(output):
    # OS-owned lock: a killed process releases it without deleting any artifacts.
    with (output / ".run.lock").open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("该目录已有评测进程，请勿同时运行。") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def client_environment(directory, info, relay):
    thinking = info.get("benchmark_thinking", False)
    if thinking and info.get("thinking") is not True:
        raise ValueError("thinking测试要求服务确认模型思考能力。")
    config = {"model": "aiserver/" + info["id"], "small_model": "aiserver/" + info["id"],
              "enabled_providers": ["aiserver"], "autoupdate": False, "snapshot": False, "share": "disabled",
              "compaction": {"auto": True, "prune": True, "reserved": min(10000, max(1024, info["limits"]["contextTokens"]//8))},
              "agent": {"title": {"disable": True}, "summary": {"disable": True}},
              "permission": {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
                             "external_directory": "deny"},
              "provider": {"aiserver": {"npm": "@ai-sdk/openai-compatible", "name": "AiServer benchmark",
                  "options": {"baseURL": relay.url, "apiKey": "{env:AISERVER_BENCHMARK_KEY}", "timeout": 3600000},
                  "models": {info["id"]: {"name": info["id"], "tool_call": info.get("toolCall", False),
                      "reasoning": info.get("thinking", False), "options": {"think": thinking},
                      "limit": model_limits(info)}}}}}
    if info.get("thinking"):
        config["provider"]["aiserver"]["models"][info["id"]]["variants"] = thinking_variants(info)
    if not info.get("toolCall", False):
        config["agent"]["benchmark-text"] = {"description": "Single file text evaluation", "mode": "primary",
            "tools": {"*": False}, "permission": {"*": "deny"},
            "prompt": "Follow the user instructions. All needed material is in the user message. Do not call tools."}
        config["permission"] = {"*": "deny"}
    path = directory / "opencode.json"
    write_json(path, config)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENCODE_", "AISERVER_"))}
    env.update(OPENCODE_CONFIG=str(path), AISERVER_BENCHMARK_KEY=relay.token,
               OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_MODELS_FETCH="true",
               OPENCODE_GIT_BASH_PATH="C:/Program Files/Git/bin/bash.exe",
               NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
    for key, name in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        folder = directory / "client" / name
        folder.mkdir(parents=True, exist_ok=True)
        env[key] = str(folder)
    return env, config, path


def phase(binary, directory, env, model, prompt, name, remaining, session=None, stop_event=None, agent="build", variant=None):
    command = [str(binary), "run", "--pure", "--agent", agent, "--format", "json", "--print-logs",
               "--model", "aiserver/" + model, "--title", directory.name]
    if session:
        command += ["--session", session]
    if variant:
        command += ["--variant", variant]
    command += [prompt]
    result = run_process(command, directory / "workspace", env, remaining, directory / name,
                         secrets=(env["AISERVER_BENCHMARK_KEY"],), stop_event=stop_event)
    events = read_events(directory / (name + ".jsonl"))
    sessions = {e.get("sessionID") or e.get("part", {}).get("sessionID") for e in events}
    sessions.discard(None)
    result.update(session_id=next(iter(sessions), None),
                  answer="\n".join(e.get("part", {}).get("text", "") for e in events if e.get("type") == "text"),
                  errors=[e.get("error") for e in events if e.get("type") == "error"],
                  session_count=len(sessions), finished=any(e.get("type") == "step_finish" and
                      e.get("part", {}).get("reason") in ("stop", "end-turn") for e in events))
    result["tools"] = []
    for event in events:
        part = event.get("part", {})
        if event.get("type") == "tool_use":
            state = part.get("state", {})
            args = state.get("input", {})
            file = args.get("filePath", args.get("path", "")) if isinstance(args, dict) else ""
            result["tools"].append({"name": part.get("tool"), "call_id": part.get("callID"),
                "status": state.get("status"), "file": Path(file).name if isinstance(file, str) else "",
                "error": str(state.get("error", ""))[:500]})
    return result


def execute_task(binary, dotnet, case, directory, info, relay, timeout, allow_correction=True):
    from csharp_benchmark.workflow import run_task
    return run_task(binary, dotnet, case, directory, info, relay, timeout,
                    client_environment, phase, allow_correction)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="新建运行目录，必须不存在")
    parser.add_argument("--resume", type=Path, help="继续未开始的题目；已开始题目不重跑")
    parser.add_argument("--report-only", type=Path, help="离线重建客户端Markdown")
    parser.add_argument("--prepare-only", action="store_true", help="只生成题目和待运行Markdown，不读取key或访问服务")
    parser.add_argument("--models", nargs="+", choices=ACTIVE_MODELS, default=list(DEFAULT_MODELS))
    parser.add_argument("--profile", choices=("roles", "calibration"), help="默认roles：9B简单/中等，27B中等/困难；calibration默认只做简单校准")
    parser.add_argument("--thinking", choices=("off", "on", "both", "fallback", "low", "medium", "high"),
                        help="默认fallback：普通模式未解决才独立重试thinking；both做全部双模式对照")
    parser.add_argument("--levels", nargs="+", choices=tuple(TIMEOUTS), help="显式覆盖计划中各模型的难度范围")
    parser.add_argument("--timeout-seconds", type=int, help="覆盖普通模式每题总限时")
    parser.add_argument("--thinking-timeout-seconds", type=int, help="覆盖thinking每题总限时，默认简单600/中等1800/困难3600秒")
    parser.add_argument("--kinds", nargs="+", choices=("read", "write"), default=["read", "write"])
    parser.add_argument("--probes", action="store_true", help="额外运行API容量探针；默认不运行")
    parser.add_argument("--no-probes", action="store_true", help="兼容旧命令，明确跳过探针")
    parser.add_argument("--first-only", action="store_true", help="禁止一次编译／功能反馈修正")
    parser.add_argument("--pause-seconds", type=float, default=2, help="独立任务间额外等待；工具续答不加间隔")
    parser.add_argument("--opencode", type=Path)
    parser.add_argument("--dotnet", default=shutil.which("dotnet") or "dotnet")
    args = parser.parse_args()
    if args.report_only:
        output = args.report_only.resolve()
        render(json.loads((output / ".state.json").read_text(encoding="utf-8")), output / "客户端报告.md")
        print(output / "客户端报告.md")
        return 0
    if args.resume and (args.output or args.prepare_only):
        parser.error("--resume不能与--output或--prepare-only同时使用")
    output = args.resume.resolve() if args.resume else (args.output or ROOT / ".local/csharp-benchmarks" /
             (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])).resolve()
    if args.resume:
        state = json.loads((output / ".state.json").read_text(encoding="utf-8"))
        validate_run(state)
        if args.thinking is not None and args.thinking != state["thinking_selection"]:
            raise ValueError("续跑不能改变thinking选项，请沿用原模式或新建运行目录。")
        if args.profile is not None and args.profile != state.get("profile"):
            raise ValueError("续跑不能改变评测计划")
        for task in state["tasks"]:
            override = args.thinking_timeout_seconds if task["thinking"] else args.timeout_seconds
            if override is not None and override != task["timeout_seconds"]:
                raise ValueError("续跑不能改变整题时间预算")
    else:
        state = prepare(output, list(dict.fromkeys(args.models)), args.levels, args.kinds, args.thinking or "fallback",
                        args.profile or "roles", args.timeout_seconds, args.thinking_timeout_seconds)
    print("运行目录：" + str(output), flush=True)
    if args.prepare_only:
        print("题目已生成；尚未连接模型。正式执行用 --resume 加此目录。")
        return 0
    with run_lock(output):
        sys.path.insert(0, str(ROOT / "scripts/client"))
        from opencode_support import BINARY, probe, saved_key
        binary = (args.opencode or BINARY).resolve()
        if not binary.is_file():
            raise ValueError("本机OpenCode程序不存在。")
        check_sdk(args.dotnet)
        version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=20)
        state["opencode_version"] = version.stdout.strip()
        record = saved_key()
        connected = probe(record)
        available = {m["id"].lower(): m for m in connected["/api/v1/models"]["models"]}
        prior_capabilities = state.get("capabilities", {})
        state["capabilities"] = bind_capabilities(state, available)
        for alias, info in state["capabilities"].items():
            state["models"][alias] = info["id"]
        if prior_capabilities and prior_capabilities != state["capabilities"] and any(t["status"] != "pending" for t in state["tasks"]):
            raise ValueError("服务发布的模型容量变化，不能混合续跑；请新建运行目录。")
        if not connected["/api/v1/ready"].get("canAcceptJobs"):
            raise ValueError("服务尚未就绪，请先检查5080。")
        if args.pause_seconds < 0:
            parser.error("--pause-seconds不能为负")
        options = {"allow_correction": not args.first_only, "probes": args.probes and not args.no_probes,
                   "pause_seconds": args.pause_seconds, "thinking": state["thinking_selection"]}
        if state.get("run_options") and state["run_options"] != options:
            raise ValueError("续跑参数变化，请沿用原参数或新建运行目录。")
        state.update(server=record["server"], status="running", run_options=options)
        for task in state["tasks"]:
            if task["status"] == "running":
                task.update(status="interrupted", result=recover_logs(output / "cases" / task["id"],
                            {"error": "上次运行中断，保留现场，不自动重跑"}))
        save(output, state)
        cases = {c.id: c for c in make_cases()}
        try:
            for alias, model in state["models"].items():
                thinking = state["conditions"][alias]["thinking"]
                info = {**state["capabilities"][alias], "benchmark_thinking": thinking}
                for task in state["tasks"]:
                    if task["model"] != alias or task["status"] != "pending":
                        continue
                    source = fallback_source(state, task)
                    if source is not None:
                        task["fallback_from"] = source["id"]
                        reason = fallback_skip_reason(source)
                        if reason:
                            task.update(status="skipped", skip_reason=reason)
                            save(output, state)
                            continue
                    print(f"[{sum(t['status'] != 'pending' for t in state['tasks'])+1}/{len(state['tasks'])}] {task['id']}", flush=True)
                    directory = output / "cases" / task["id"]
                    frozen = json.loads((directory / "frozen-task.json").read_text(encoding="utf-8"))
                    for name, expected_hash in (("Script.cs", frozen["initial_sha256"]), ("TASK.md", frozen["task_sha256"])):
                        if sha((directory / "workspace" / name).read_text(encoding="utf-8")) != expected_hash:
                            raise ValueError("未开始题目的初始文件已变化，停止以避免不公平比较；请新建运行目录。")
                    if args.pause_seconds:
                        time.sleep(args.pause_seconds)
                    wait_ready(record["server"], record["apiKey"])
                    task.update(status="running", started_at=utc())
                    save(output, state)
                    try:
                        with Recorder(record["server"], record["apiKey"], directory / "http", model,
                                      expected_thinking=thinking) as relay:
                            task["result"] = execute_task(binary, args.dotnet, cases[task["case"]], directory, info, relay,
                                                          task["timeout_seconds"], not args.first_only)
                        task["status"] = "passed" if task["result"].get("task_passed") else "failed"
                    except KeyboardInterrupt:
                        task.update(status="interrupted", result=recover_logs(directory,
                                    {"error": "用户中断；日志和脚本已保存"}))
                        raise
                    except Exception as error:
                        task.update(status="failed", result=recover_logs(directory, {"error": type(error).__name__}))
                    task["finished_at"] = utc()
                    recover_logs(directory, task["result"])
                    task["deployment_phase"] = "cold-or-switch" if not any(
                        t["model"] == alias and t["status"] != "pending" and t is not task for t in state["tasks"]) else "warm-followup"
                    save(output, state)
                if args.probes and not args.no_probes and not state.get("probes_completed", {}).get(alias):
                    work = output / "context" / alias
                    work.mkdir(parents=True, exist_ok=True)
                    previous = read_events(work / "probes.jsonl")
                    def publish(rows):
                        state["probes"][alias] = list(rows)
                        save(output, state)
                    with Recorder(record["server"], record["apiKey"], work / "http", model,
                                  expected_thinking=thinking) as relay:
                        state["probes"][alias] = exercise(relay, info, work / "probes.jsonl", previous, publish)
                    state.setdefault("probes_completed", {})[alias] = len({r["id"] for r in state["probes"][alias] if not r["id"].startswith("refine-")}) >= 8
                    save(output, state)
            state["status"] = "finished"
        except KeyboardInterrupt:
            state["status"] = "interrupted"
        except Exception:
            state["status"] = "failed"
            raise
        finally:
            save(output, state)
    print("客户端Markdown：" + str(output / "客户端报告.md"), flush=True)
    return 0 if state["status"] == "finished" else 130


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print("启动失败：" + str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # Request/DPAPI exceptions must never print authenticated objects or tracebacks.
        message = str(error) if type(error).__name__ == "SetupError" else type(error).__name__
        print("评测失败：" + message, file=sys.stderr)
        raise SystemExit(1)

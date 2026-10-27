"""Real OpenCode + real AiServer HTTP/SSE, with synthetic inference on loopback.

Uses a fresh client data directory, temporary database and synthetic API key.
Never reads the saved key, calls the public service, or loads a GPU model.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))


@contextmanager
def fixture(work, mode):
    import uvicorn
    from aiserver.app import create_app
    from aiserver.context.window_budget import budget_details
    from aiserver.inference.contracts import Generation, ModelError, Profile
    from aiserver.inference.runtime import PracticalBudget
    from aiserver.settings import Settings

    cfg = {"Model": "context-fixture", "NumCtx": 16384, "NumPredict": 1024, "ReservedTokens": 512,
           "MaxInputTokens": 16, "ImagePolicy": {"MaxImages": 0}, "MaxTextUtf8Bytes": 2097152}
    state = {"mode": mode, "overflow": False, "summary": False, "requests": []}
    profile = Profile("context-fixture", "Fake", json.dumps({"config": cfg}), ("text",), True)

    class Counter:
        async def count(self, prompt):
            text = " ".join(m["content"] for m in prompt.messages())
            if mode == "preflight" and "TRIGGER_OVERFLOW_PROBE" in text and not state["overflow"]:
                state["overflow"] = True
                state["requests"].append("preflight-overflow")
                return budget_details(cfg, prompt)["inputLimitTokens"] + 1
            return 256

    budget = PracticalBudget(cfg)
    budget.counter = Counter()

    class Provider:
        supports_tools = True
        async def generate(self, material, stop, on_delta):
            prompt = json.loads(material.payload_json)
            text = " ".join(m["content"] for m in json.loads(prompt["messages_json"]))
            if mode == "engine" and "TRIGGER_OVERFLOW_PROBE" in text and not state["overflow"]:
                state["overflow"] = True
                state["requests"].append("engine-overflow")
                raise ModelError("CONTEXT_TOO_LARGE", request_finished=True)
            if state["overflow"] and not state["summary"]:
                state["summary"] = True
                state["requests"].append("summary-response")
                answer = "Earlier user asked for a seed reply. Now continue with the pending request. Context is compacted."
            elif state["summary"]:
                state["requests"].append("resumed-response")
                answer = "COMPACTION_RETRY_OK"
            else:
                state["requests"].append("seed-response")
                answer = "SEED_READY"
            await on_delta(answer)
            return Generation(answer, True, "stop", input_tokens=256, output_tokens=16)
        async def aclose(self):
            pass

    app = create_app(Settings(data_directory=work / "server", requests_per_minute=10000,
                              submissions_per_minute=1000, manage_natapp=False),
                     profile=profile, budget=budget, provider=Provider())
    store = app.state.store
    store.initialize()
    store.user_add("fixture", "Synthetic fixture")
    key = store.token_create("fixture")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    until = time.monotonic() + 10
    while not server.started and time.monotonic() < until:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("Loopback fixture did not start")
    try:
        yield f"http://127.0.0.1:{port}/v1", key, state
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("Loopback fixture did not close")


def client_environment(work, url, key):
    cfg = {"$schema": "https://opencode.ai/config.json", "model": "aiserver/context-fixture",
           "small_model": "aiserver/context-fixture", "enabled_providers": ["aiserver"],
           "autoupdate": False, "snapshot": False, "share": "disabled",
           "permission": {"*": "deny"}, "compaction": {"auto": True, "prune": True, "reserved": 1000},
           "agent": {"title": {"disable": True}, "summary": {"disable": True}},
           "provider": {"aiserver": {"npm": "@ai-sdk/openai-compatible", "name": "Synthetic AiServer",
               "options": {"baseURL": url, "apiKey": "{env:AISERVER_CONTEXT_TEST_KEY}"},
               "models": {"context-fixture": {"name": "Synthetic context fixture", "tool_call": True,
                   "limit": {"context": 16384, "input": 14848, "output": 1024}}}}}}
    path = work / "opencode.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    env = os.environ.copy()
    for name in ("OPENCODE_CONFIG_CONTENT", "AISERVER_API_KEY"):
        env.pop(name, None)
    env.update(OPENCODE_CONFIG=str(path), AISERVER_CONTEXT_TEST_KEY=key,
               OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_MODELS_FETCH="true",
               OPENCODE_GIT_BASH_PATH="C:/Program Files/Git/bin/bash.exe",
               NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
    for name, directory in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                            ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        target = work / directory
        target.mkdir(parents=True)
        env[name] = str(target)
    return env


def run_case(binary, work, mode):
    work.mkdir(parents=True)
    with fixture(work, mode) as (url, key, state):
        env = client_environment(work, url, key)
        codes = {}
        def run(arguments, name):
            result = subprocess.run([str(binary), *arguments], cwd=work, env=env, timeout=120,
                                    capture_output=True, text=True, encoding="utf-8")
            # All text is synthetic; the key is never written to captured artifacts.
            output = result.stdout.replace(key, "[redacted]")
            errors = result.stderr.replace(key, "[redacted]")
            codes[name] = result.returncode
            (work / (name + ".jsonl")).write_text(output, encoding="utf-8")
            (work / (name + ".stderr.txt")).write_text(errors, encoding="utf-8")
            if result.returncode and name != "recovery":
                raise RuntimeError(f"OpenCode {name} failed; inspect the isolated stderr artifact")
            return output
        first = run(["run", "--pure", "--format", "json", "--title", "Context recovery fixture",
                     "Reply SEED_READY. Do not use tools."], "seed")
        events = [json.loads(line) for line in first.splitlines() if line.startswith("{")]
        session = next((e.get("sessionID") or e.get("part", {}).get("sessionID") for e in events
                        if e.get("sessionID") or e.get("part", {}).get("sessionID")), None)
        if not session:
            raise RuntimeError("OpenCode did not return a fixture session ID")
        resumed = run(["run", "--pure", "--format", "json", "--session", session,
                       "TRIGGER_OVERFLOW_PROBE: continue and reply COMPACTION_RETRY_OK. Do not use tools."], "recovery")
        exported = json.loads(run(["export", session], "export"))
        messages = exported.get("messages", [])
        compactions = [part for message in messages for part in message.get("parts", [])
                       if part.get("type") == "compaction"]
        summaries = [m for m in messages if m.get("info", {}).get("summary")]
        recovery_events = [json.loads(line) for line in resumed.splitlines() if line.startswith("{")]
        errors = [e for e in recovery_events if e.get("type") == "error"]
        only_overflow_errors = all(e.get("error", {}).get("name") == "ContextOverflowError" for e in errors)
        result = {"mode": mode, "requests": state["requests"], "compaction_parts": len(compactions),
                  "summary_messages": len(summaries), "continued": "COMPACTION_RETRY_OK" in resumed,
                  "recovery_exit_code": codes["recovery"], "overflow_notifications": len(errors)}
        result["passed"] = bool(state["overflow"] and compactions and summaries and result["continued"]
                                and "resumed-response" in state["requests"] and only_overflow_errors)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, default=ROOT / ".local/apps/opencode/node_modules/opencode-ai/bin/opencode.exe")
    parser.add_argument("--mode", choices=("preflight", "engine", "both"), default="both")
    args = parser.parse_args()
    work = ROOT / ".local/test-runs" / ("opencode-context-" + uuid4().hex)
    work.mkdir(parents=True)
    report = {"kind": "real-opencode-synthetic-model-loopback-only", "checks": [], "passed": False}
    try:
        version = subprocess.run([str(args.opencode), "--version"], capture_output=True, text=True, timeout=20)
        report["opencode_version"] = version.stdout.strip()
        for mode in (("preflight", "engine") if args.mode == "both" else (args.mode,)):
            report["checks"].append(run_case(args.opencode, work / mode, mode))
        report["passed"] = all(item["passed"] for item in report["checks"])
    except Exception as error:
        report["error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    (work / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Isolated artifacts: " + str(work))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

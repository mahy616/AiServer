"""Real .NET evaluation and real OpenCode against a loopback synthetic model only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/validation"))
from csharp_benchmark.cases import analysis_result, make_cases
from csharp_benchmark.common import read_events, redacted_text, run_process, sha, write_json
from csharp_benchmark.harness import evaluate
from csharp_benchmark.relay import Recorder, StreamFacts, completion
from csharp_benchmark.probes import exercise, material
from types import SimpleNamespace


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/validation" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("csharp_runner", "Benchmark-CSharp.py")
collector = load("csharp_collector", "Collect-CSharpBenchmark.py")
CASES = make_cases()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_real_dotnet_reference_passes_independent_oracle(tmp_path, case):
    if not shutil.which("dotnet"):
        pytest.skip(".NET SDK unavailable")
    script = tmp_path / "Reference.cs"
    script.write_text(case.reference, encoding="utf-8")
    result = evaluate(case, script, tmp_path / "evaluation")
    assert result.get("all_passed"), result
    assert len(case.source.splitlines()) <= case.cap


@pytest.mark.parametrize("case", [c for c in CASES if c.kind == "read"], ids=lambda c: c.id)
def test_seeded_faults_are_detected_by_real_numeric_tests(tmp_path, case):
    script = tmp_path / "Script.cs"
    script.write_text(case.source, encoding="utf-8")
    result = evaluate(case, script, tmp_path / "evaluation")
    assert result["compiled"] and result["passed"] < result["total"]
    assert analysis_result(json.dumps({"faultyFunctions": case.faulty, "sampleResult": case.sample_result}), case) == {
        "faults_correct": True, "sample_correct": True}


def test_evaluator_catches_input_mutation_and_compile_error(tmp_path):
    case = next(c for c in CASES if c.id == "easy-write")
    script = tmp_path / "Script.cs"
    script.write_text(case.reference.replace("double weight=0", "if(input.Length>0) input[0]=999;\n        double weight=0"))
    assert not evaluate(case, script, tmp_path / "mutating")["all_passed"]
    script.write_text("not C#")
    assert not evaluate(case, script, tmp_path / "broken")["compiled"]


def test_preparation_isolated_reproducible_and_only_markdown_report(tmp_path):
    state = runner.prepare(tmp_path / "run", ["7b", "14b", "9b"], ["easy", "medium", "hard"], ["read", "write"])
    assert len(state["tasks"]) == 18
    assert len({t["id"] for t in state["tasks"]}) == 18
    assert runner.suite_hash() == state["suite_sha256"]
    assert (tmp_path / "run/客户端报告.md").is_file()
    assert not list((tmp_path / "run").glob("*.html"))
    assert not list((tmp_path / "run").glob("*.csv"))
    assert len(list((tmp_path / "run/cases").glob("*/workspace/Script.cs"))) == 18


def test_stream_parser_split_lines_usage_and_context_errors():
    facts = StreamFacts()
    data = 'data: {"id":"chatcmpl-job","choices":[{"delta":{"content":"hello"}}]}\n\n'
    for char in data:
        facts.feed(char, .5)
    facts.feed('data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":1}}\n\ndata: [DONE]\n\n', 1)
    assert facts.job_id == "job" and facts.text == "hello" and facts.done
    assert facts.usage["prompt_tokens"] == 7 and facts.first_text == .5


def test_probe_budget_boundaries_and_actual_usage_not_estimates(tmp_path, monkeypatch):
    calls = []
    def fake_completion(url, token, body, timeout):
        assert not relay.terminal_error.is_set()
        prompt = body["messages"][0]["content"]
        calls.append(body)
        if len(prompt)/2+160 > 20000-body["max_tokens"]-512:
            relay.terminal_error.set()
            return {"http_status": 400, "answer": "", "error": {"code": "context_length_exceeded"}}
        return {"http_status": 200, "done": True, "answer": json.dumps({"start":137,"middle":293,"end":419,"computed":40560}),
                "usage": {"prompt_tokens": len(prompt)//3,"completion_tokens":20}, "finish_reason":"stop"}
    monkeypatch.setattr("csharp_benchmark.probes.completion", fake_completion)
    relay = SimpleNamespace(model="synthetic",url="http://127.0.0.1",token="synthetic",terminal_error=threading.Event())
    info = {"limits":{"contextTokens":20000,"maxOutputTokens":8192,"reservedTokens":512,"defaultMaxInputTokens":11296},
            "capacityMethod":"conservative-estimate-v1"}
    rows = exercise(relay, info, tmp_path/"probes.jsonl")
    assert 9 <= len(calls) <= 12
    assert any(r.get("answer_correct") for r in rows)
    assert any(r.get("http_status") == 400 for r in rows)
    assert rows[0]["actual_input_tokens"] != rows[0]["target_tokens"]
    assert all(r["actual_input_tokens"] == r["input_characters"]//3 for r in rows if r.get("actual_input_tokens"))
    for size in (500, 1000, 200000):
        assert len(material(size)[0]) == size


def test_chunked_redaction_and_timed_process_keeps_partial_logs(tmp_path):
    secret = "ais_" + "a"*32 + "_synthetic_only"
    with redacted_text(tmp_path/"stream.txt", (secret,)) as output:
        for char in "before " + secret + " after\n":
            output.write(char)
    assert (tmp_path/"stream.txt").read_text() == "before [redacted] after\n"
    import os
    result = run_process([sys.executable, "-c", "import time;print('"+secret+"',flush=True);time.sleep(20)"],
                         tmp_path, dict(os.environ), .5, tmp_path/"timeout", (secret,))
    assert result["timed_out"]
    assert "[redacted]" in (tmp_path/"timeout.jsonl").read_text()
    assert secret not in (tmp_path/"timeout.jsonl").read_text()


def test_collector_cli_archives_only_raw_data_and_preserves_db(tmp_path):
    import subprocess
    config, inference = tmp_path/"server.json", tmp_path/"model.json"
    data = tmp_path/"data"
    data.mkdir()
    database = data/"jobs.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("CREATE TABLE task_performance(job_id TEXT,user_id TEXT,created_at_ms INTEGER);"
                           "CREATE TABLE jobs(job_id TEXT,profile_id TEXT);")
    before = database.read_bytes()
    write_json(inference, {"Model":"synthetic", "KvCacheType":"q8_0", "ApiKey":"PRIVATE_SENTINEL"})
    write_json(config, {"provider":"Fake","data_directory":str(data),"inference_config":str(inference),"password":"PRIVATE_SENTINEL"})
    result = subprocess.run([sys.executable,str(ROOT/"scripts/validation/Collect-CSharpBenchmark.py"),
                             "--server-config",str(config),"--output",str(tmp_path/"capture"),"--duration","1","--interval","0.2"],
                            capture_output=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert (tmp_path/"capture.zip").is_file()
    assert database.read_bytes() == before
    files = list((tmp_path/"capture").iterdir())
    assert not any(p.suffix == ".md" for p in files)
    assert "PRIVATE_SENTINEL" not in "".join(p.read_text(encoding="utf-8") for p in files)


@contextmanager
def synthetic_api(responder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            events = responder(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for event in events:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def event(model, delta, reason=None):
    return {"id": "chatcmpl-synthetic-job", "object": "chat.completion.chunk", "created": 1, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}


def test_live_stream_relay_preserves_early_output_and_job_correlation(tmp_path):
    release = threading.Event()
    model = "synthetic"
    def responder(body):
        yield event(model, {"content": "early"})
        assert release.wait(5)
        yield event(model, {}, "stop")
    with synthetic_api(responder) as server, Recorder(server, "synthetic-private-key", tmp_path, model) as relay:
        with httpx.Client(timeout=5, trust_env=False) as client:
            with client.stream("POST", relay.url+"/chat/completions", headers={"Authorization": "Bearer "+relay.token},
                               json={"model": model, "stream": True, "messages": []}) as response:
                try:
                    lines = response.iter_lines()
                    first = next(lines)
                    assert "early" in first
                finally:
                    release.set()
                list(lines)
        for _ in range(50):
            if relay.rows:
                break
            time.sleep(.01)
        assert relay.rows[0]["job_id"] == "synthetic-job"
        assert "synthetic-private-key" not in "".join(p.read_text() for p in tmp_path.iterdir())


def test_collector_read_only_task_filter_and_log_rotation(tmp_path):
    database = tmp_path / "jobs.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("CREATE TABLE task_performance(job_id TEXT,user_id TEXT,created_at_ms INTEGER,status TEXT);"
                           "CREATE TABLE jobs(job_id TEXT,profile_id TEXT);"
                           "INSERT INTO task_performance VALUES('a','mine',100,'running'),('b','other',110,'running');"
                           "INSERT INTO jobs VALUES('a','7b'),('b','9b');")
    before = database.read_bytes()
    assert collector.task_rows(database, 90, "mine")[0]["job_id"] == "a"
    assert database.read_bytes() == before
    assert collector.task_rows(tmp_path / "absent.db", 0) == []
    assert not (tmp_path / "absent.db").exists()
    log, output = tmp_path / "engine.log", tmp_path / "collected.jsonl"
    log.write_text("initial\n")
    tail = collector.LogTail(log, output)
    tail.poll()
    with log.open("a") as stream:
        stream.write("before rotation\n")
    log.rename(tmp_path / "engine.log.1")
    log.write_text("after rotation\n")
    tail.poll()
    tail.close()
    assert [r["line"] for r in read_events(output)] == ["initial", "before rotation", "after rotation"]


@pytest.mark.parametrize("needs_fix", [False, True])
@pytest.mark.parametrize("thinking", [False, True, "low", "high"])
def test_real_opencode_read_write_single_session_against_synthetic_model(tmp_path, needs_fix, thinking):
    binary = ROOT / ".local/apps/opencode/node_modules/opencode-ai/bin/opencode.exe"
    if not binary.is_file() or not shutil.which("dotnet"):
        pytest.skip("Requires installed OpenCode and .NET SDK")
    named = isinstance(thinking, str)
    state = runner.prepare(tmp_path / "run", ["gptoss" if named else "9b"], ["easy"], ["read"],
                           thinking=thinking if named else "on" if thinking else "off")
    task = state["tasks"][0]
    case = next(c for c in CASES if c.id == task["case"])
    directory = tmp_path / "run/cases" / task["id"]
    model = state["models"][task['model']]
    tool_names = []
    def responder(body):
        assert body.get("think") == thinking
        if thinking:
            yield event(model, {"reasoning_content": "SYNTHETIC_THOUGHT"})
        is_reading = relay.phase == "reading"
        names = [t["function"]["name"] for t in body.get("tools", [])]
        tool_names.append(names)
        if body["messages"][-1]["role"] != "tool":
            name = "read" if is_reading else "write"
            assert name in names, names
            arguments = {"filePath": str(directory / "workspace/Script.cs")}
            if not is_reading:
                arguments["content"] = "public static class Script { BROKEN }" if needs_fix and relay.phase == "implementation" else case.reference
            call = {"index": 0, "id": "call_" + str(len(tool_names)), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}
            yield event(model, {"role": "assistant", "tool_calls": [call]}, "tool_calls")
        else:
            answer = json.dumps({"faultyFunctions": case.faulty, "sampleResult": case.sample_result}) if is_reading else "首次实现已完成"
            yield event(model, {"role": "assistant", "content": answer})
            yield event(model, {}, "stop")
        yield {"id": "chatcmpl-synthetic-job", "object": "chat.completion.chunk", "created": 1, "model": model,
               "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
    info = {"id": model, "toolCall": True, "thinking": True, "benchmark_thinking": thinking,
            "limits": {"contextTokens": 81920, "maxInputTokens": 81407, "maxOutputTokens": 8192}}
    if named:
        info.update(thinkingLevels=['low', 'medium', 'high'], defaultThinkingLevel='medium')
    with synthetic_api(responder) as server, Recorder(server, "synthetic-not-a-personal-key", directory / "http", model,
                                                     expected_thinking=thinking) as relay:
        result = runner.execute_task(binary, shutil.which("dotnet"), case, directory, info, relay, 120)
    assert not result.get("error"), result
    assert result["analysis"]["faults_correct"] and result["analysis"]["sample_correct"]
    assert result["evaluation"]["all_passed"], result
    assert len({p["session_id"] for p in result["phases"]}) == 1
    assert len(result["requests"]) == (6 if needs_fix else 4)
    assert result["correction_used"] is needs_fix
    assert result["first"]["evaluation"]["all_passed"] is not needs_fix
    assert "write" not in tool_names[0] and "bash" not in tool_names[0]
    assert result["thinking"] == thinking
    assert all(r["requested_thinking"] == thinking and r["expected_thinking"] == thinking for r in result["requests"])
    assert all((r["reasoning_chars"] > 0) is bool(thinking) for r in result["requests"])


def test_available_models_and_gptoss_levels_are_independent(tmp_path):
    state = runner.prepare(tmp_path / 'run', list(runner.ACTIVE_MODELS), ['easy'], ['write'], thinking='both')
    runner.validate_run(state)
    assert len(state['conditions']) == 15
    assert [c['thinking'] for c in state['conditions'].values() if c['base_alias'] == 'gptoss'] == ['low', 'medium', 'high']
    assert len({t['id'] for t in state['tasks']}) == 15
    assert [c['thinking'] for c in state['conditions'].values() if c['base_alias'] == 'strata'] == [False, True]
    assert [c['thinking'] for c in state['conditions'].values() if c['base_alias'] == 'deepseek'] == [False, True]
    with pytest.raises(ValueError, match='不能关闭'):
        runner.prepare(tmp_path / 'invalid', ['gptoss'], ['easy'], ['write'], thinking='off')
    state = runner.prepare(tmp_path / 'fallback', ['gptoss'], ['easy'], ['write'], thinking='fallback')
    assert runner.fallback_source(state, state['tasks'][0]) is None


def test_real_opencode_text_only_model_does_not_require_tools(tmp_path):
    binary = ROOT / ".local/apps/opencode/node_modules/opencode-ai/bin/opencode.exe"
    if not binary.is_file() or not shutil.which("dotnet"):
        pytest.skip("Requires OpenCode and .NET")
    state = runner.prepare(tmp_path/"run", ["7b"], ["easy"], ["write"])
    task = state["tasks"][0]
    case = next(c for c in CASES if c.id == task["case"])
    directory = tmp_path/"run/cases"/task["id"]
    model = state["models"]["7b"]
    seen = []
    def responder(body):
        assert not body.get("tools")
        seen.append(body)
        yield event(model, {"role":"assistant", "content":"```csharp\n"+case.reference+"\n```"})
        yield event(model, {}, "stop")
    info = {"id":model, "toolCall":False, "limits":{"contextTokens":16384,"maxInputTokens":15871,"maxOutputTokens":4096}}
    with synthetic_api(responder) as server, Recorder(server,"synthetic-key",directory/"http",model) as relay:
        result = runner.execute_task(binary,shutil.which("dotnet"),case,directory,info,relay,60)
    assert not result.get("error"), result.get("error")
    assert result["evaluation"]["all_passed"] and result["mode"] == "text-code"
    assert result["first"]["text_saved_by_harness"] and not result["first"]["write_tool_completed"]
    assert len(seen) == 1


@pytest.mark.parametrize("kind", ["explicit_error", "empty_completion", "incomplete_stream", "thinking_truncated"])
def test_real_opencode_stops_at_first_service_error(tmp_path, kind):
    binary = ROOT / ".local/apps/opencode/node_modules/opencode-ai/bin/opencode.exe"
    if not binary.is_file() or not shutil.which("dotnet"):
        pytest.skip("Requires OpenCode and .NET")
    state = runner.prepare(tmp_path/"run", ["7b"], ["easy"], ["write"])
    task = state["tasks"][0]
    case = next(c for c in CASES if c.id == task["case"])
    directory = tmp_path/"run/cases"/task["id"]
    model = state["models"]["7b"]
    seen = []
    def responder(body):
        seen.append(body)
        if kind == "explicit_error":
            yield {"error":{"code":"MODEL_EMPTY_OUTPUT","message":"Model empty output","type":"server_error"}}
        elif kind == "thinking_truncated":
            assert body.get("think") is True
            yield event(model, {"reasoning_content": "SYNTHETIC_PARTIAL_THOUGHT"})
            data = (ROOT / "contracts/examples/openai-truncated-stream-error.sse").read_text(encoding="utf-8")
            yield json.loads(data.removeprefix("data: ").strip())
        else:
            # Closing an HTTP 200 stream without any model output is also an
            # unusable completion, not permission for an unbounded retry loop.
            yield {}
            if kind == "incomplete_stream":
                raise ConnectionResetError("synthetic truncated stream")
    thinking = kind == "thinking_truncated"
    info = {"id":model,"toolCall":True,"thinking":True,"benchmark_thinking":thinking,
            "limits":{"contextTokens":81920,"maxInputTokens":81407,"maxOutputTokens":8192}}
    with synthetic_api(responder) as server, Recorder(server,"synthetic-key",directory/"http",model,expected_thinking=thinking) as relay:
        result = runner.execute_task(binary,shutil.which("dotnet"),case,directory,info,relay,60)
    assert result.get("error") and not result["correction_used"]
    assert len(seen) == 1 and not result["evaluation"]["all_passed"]
    assert not result["phases"][0]["timed_out"]
    if thinking:
        assert result["requests"][0]["reasoning_chars"] > 0
        assert "OUTPUT_TRUNCATED" in result["requests"][0]["error"]


def test_default_role_plan_and_old_deepseek_run_cannot_resume(tmp_path, monkeypatch):
    output = tmp_path / "qwen2"
    monkeypatch.setattr(sys, "argv", ["Benchmark-CSharp.py", "--prepare-only", "--output", str(output)])
    assert runner.main() == 0
    state = json.loads((output / ".state.json").read_bytes())
    assert set(state["models"]) == {"9b", "qwen38", "9b-thinking", "qwen38-thinking"}
    assert len(set(state["models"].values())) == 2 and len(state["tasks"]) == 16
    assert state["profile"] == "roles" and state["thinking_selection"] == "fallback"
    assert {t["case"].split("-")[0] for t in state["tasks"] if t["model"] == "9b"} == {"easy", "medium"}
    assert {t["case"].split("-")[0] for t in state["tasks"] if t["model"] == "qwen38"} == {"medium", "hard"}
    state["models"]["retired-model"] = "retired-model"
    (output / ".state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["Benchmark-CSharp.py", "--resume", str(output)])
    with pytest.raises(ValueError, match="已移除模型"):
        runner.main()


def test_calibration_keeps_twelve_conditions_and_longer_thinking_budgets(tmp_path):
    state = runner.prepare(tmp_path / "run", ["7b", "14b", "9b", "qwen38"], None, ["read", "write"], "both", "calibration")
    assert len(state["tasks"]) == 12 and all(t["case"].startswith("easy-") for t in state["tasks"])
    assert {t["timeout_seconds"] for t in state["tasks"] if t["thinking"]} == {600}
    assert {t["timeout_seconds"] for t in state["tasks"] if not t["thinking"]} == {180}
    normal, thinking = [t for t in state["tasks"] if t["case"] == "easy-read" and t["model"].startswith("9b")]
    state["thinking_selection"] = "fallback"
    assert runner.fallback_source(state, thinking) is normal
    normal["status"] = "passed"
    assert "已解决" in runner.fallback_skip_reason(normal)
    normal.update(status="failed", result={"failure_category": "analysis"})
    assert runner.fallback_skip_reason(normal) is None
    normal["result"]["failure_category"] = "transport-or-service"
    assert "基础链路失败" in runner.fallback_skip_reason(normal)


def test_partial_usage_cannot_look_like_the_whole_task_cost():
    from csharp_benchmark.report import usage
    assert "不完整" in usage([{"usage": {"prompt_tokens": 10, "completion_tokens": 20}}, {"usage": None}])
    assert usage([{"usage": {"prompt_tokens": 10, "completion_tokens": 20}}]) == "10/20"


def test_budget_exhaustion_is_distinct_from_a_transport_failure(tmp_path, monkeypatch):
    state = runner.prepare(tmp_path / "run", ["9b"], ["easy"], ["read"])
    directory = tmp_path / "run/cases" / state["tasks"][0]["id"]
    model = state["models"]["9b"]
    info = {"id": model, "toolCall": True, "thinking": True,
            "limits": {"contextTokens": 81920, "maxInputTokens": 81407, "maxOutputTokens": 8192}}
    monkeypatch.setattr(runner, "phase", lambda *a, **kw: {"timed_out": True, "session_id": None, "finished": False})
    monkeypatch.setattr("csharp_benchmark.workflow.evaluate", lambda *a, **kw: {"compiled": True, "passed": 5, "total": 19})
    with Recorder("http://127.0.0.1:1", "synthetic", directory / "http", model) as relay:
        result = runner.execute_task(Path("unused"), "unused", next(c for c in CASES if c.id == "easy-read"), directory, info, relay, 1)
    assert result["failure_category"] == "budget"
    assert "整题时间预算" in result["error"] and not result["correction_used"]


def test_reading_analysis_gets_one_correction_even_when_code_already_passes(tmp_path, monkeypatch):
    if not shutil.which("dotnet"):
        pytest.skip("Requires .NET SDK")
    state = runner.prepare(tmp_path / "run", ["9b"], ["easy"], ["read"])
    directory = tmp_path / "run/cases" / state["tasks"][0]["id"]
    case = next(c for c in CASES if c.id == "easy-read")
    model = state["models"]["9b"]
    info = {"id": model, "toolCall": True, "limits": {"contextTokens": 81920, "maxInputTokens": 81407, "maxOutputTokens": 8192}}
    def phase(binary, work, env, model, prompt, name, *a, **kw):
        tools = []
        if name == "implementation":
            (directory / "workspace/Script.cs").write_text(case.reference)
            tools = [{"name": "write", "file": "Script.cs", "status": "completed"}]
        elif name == "reading":
            tools = [{"name": "read", "file": "Script.cs", "status": "completed"}]
        if name == "correction":
            assert case.source in prompt
            assert json.loads((directory / "opencode.json").read_bytes())["permission"]["write"] == "deny"
        answer = json.dumps({"faultyFunctions": case.faulty, "sampleResult": [] if name == "reading" else case.sample_result})
        return {"session_id": "same-session", "session_count": 1, "finished": True, "answer": answer, "tools": tools}
    monkeypatch.setattr(runner, "phase", phase)
    monkeypatch.setattr("csharp_benchmark.workflow.run_process", lambda *a, **kw: {"exit_code": 1})
    with Recorder("http://127.0.0.1:1", "synthetic", directory / "http", model) as relay:
        result = runner.execute_task(Path("unused"), shutil.which("dotnet"), case, directory, info, relay, 60)
    assert result["task_passed"] and result["correction_used"]
    assert not result["first"]["task_passed"] and result["first"]["evaluation"]["all_passed"]


@pytest.mark.parametrize("selection,count", [("off", 8), ("on", 4), ("both", 12)])
def test_thinking_plans_keep_identical_material_and_separate_sessions(tmp_path, selection, count):
    state = runner.prepare(tmp_path / "run", ["7b", "14b", "9b", "qwen38"], ["easy"], ["read", "write"], thinking=selection)
    assert len(state["tasks"]) == count
    runner.validate_run(state)
    if selection == "both":
        for base in ("9b", "qwen38"):
            for kind in ("read", "write"):
                for name in ("TASK.md", "Script.cs"):
                    left = tmp_path / f"run/cases/{base}-easy-{kind}/workspace/{name}"
                    right = tmp_path / f"run/cases/{base}-thinking-easy-{kind}/workspace/{name}"
                    assert left.read_bytes() == right.read_bytes() and left != right


def test_thinking_capability_and_resume_settings_cannot_silently_fall_back(tmp_path):
    state = runner.prepare(tmp_path / "run", ["9b"], ["easy"], ["write"], thinking="both")
    model = state["models"]["9b"]
    with pytest.raises(ValueError, match="thinking能力"):
        runner.bind_capabilities(state, {model.lower(): {"id": model, "thinking": False}})
    assert len(runner.bind_capabilities(state, {model.lower(): {"id": model, "thinking": True}})) == 2
    state["conditions"]["9b-thinking"]["thinking"] = False
    with pytest.raises(ValueError, match="思考模式发生变化"):
        runner.validate_run(state)
    state["version"] = "csharp-scripts-v2"
    with pytest.raises(ValueError, match="不能混合续跑"):
        runner.validate_run(state)
    with pytest.raises(ValueError, match="没有thinking测试项"):
        runner.prepare(tmp_path / "unsupported", ["7b", "14b"], ["easy"], ["write"], thinking="on")
    assert not (tmp_path / "unsupported").exists()


@pytest.mark.parametrize("actual", [False, None])
def test_relay_rejects_wrong_or_missing_think_before_reaching_model(tmp_path, actual):
    seen = []
    def responder(body):
        seen.append(body)
        yield event("synthetic", {"content": "should not reach model"})
    with synthetic_api(responder) as server, Recorder(server, "synthetic", tmp_path, "synthetic", expected_thinking=True) as relay:
        body = {"model": "synthetic", "messages": [], "stream": True}
        if actual is not None:
            body["think"] = actual
        result = completion(relay.url, relay.token, body, 5)
    assert result["http_status"] == 400 and not seen
    assert "BENCHMARK_THINKING_MISMATCH" in relay.rows[0]["error"]
    assert relay.rows[0]["requested_thinking"] is actual


def test_report_compares_thinking_cost_only_on_shared_successes(tmp_path):
    state = runner.prepare(tmp_path / "run", ["9b"], ["easy"], ["write"], thinking="both")
    for index, task in enumerate(state["tasks"]):
        task.update(status="passed", result={"task_passed": True, "delivery_valid": True,
            "mode": "native-tools", "seconds": 2 + index, "evaluation": {"all_passed": True},
            "first": {"task_passed": True}, "requests": [{"usage": {"prompt_tokens": 10, "completion_tokens": 20 + index},
                "reasoning_chars": 50 * index, "requested_thinking": bool(index), "expected_thinking": bool(index)}]})
    runner.save(tmp_path / "run", state)
    report = (tmp_path / "run/客户端报告.md").read_text(encoding="utf-8")
    assert "| 9b | 1→1/1 | 1→1/1 | 1 | 2.00／3.00 | 20／21 | 50 |" in report
    state["tasks"][1]["result"].update(task_passed=False, error="OUTPUT_TRUNCATED")
    runner.save(tmp_path / "run", state)
    assert "| 9b | 1→1/1 | 1→0/1 | 0 | —／— | 未取得／未取得 | — |" in (tmp_path / "run/客户端报告.md").read_text(encoding="utf-8")

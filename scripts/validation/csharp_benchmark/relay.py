"""Loopback-only streaming recorder. The real API key never enters OpenCode."""
import codecs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import httpx

from .common import append_json, redact, redacted_text, sha, utc, write_json


class StreamFacts:
    def __init__(self):
        self.buffer = ""
        self.job_id = None
        self.usage = None
        self.error = None
        self.finish = None
        self.done = False
        self.text = ""
        self.first_content = None
        self.first_text = None
        self.first_reasoning = None
        self.reasoning_chars = 0
        self.tools = {}

    def feed(self, text, elapsed):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                self.done = True
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if event.get("id", "").startswith("chatcmpl-"):
                self.job_id = event["id"][9:]
            if event.get("usage"):
                self.usage = event["usage"]
            if event.get("error"):
                self.error = event["error"]
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    if self.first_reasoning is None:
                        self.first_reasoning = elapsed
                    self.reasoning_chars += len(reasoning)
                for tool in delta.get("tool_calls", []):
                    index = tool.get("index", 0)
                    item = self.tools.setdefault(index, {"id": "", "name": ""})
                    item["id"] = tool.get("id") or item["id"]
                    item["name"] += tool.get("function", {}).get("name", "")
                if delta.get("content") or delta.get("tool_calls"):
                    if self.first_content is None:
                        self.first_content = elapsed
                if delta.get("content"):
                    if self.first_text is None:
                        self.first_text = elapsed
                    self.text += delta["content"]
                self.finish = choice.get("finish_reason") or self.finish


class Recorder:
    def __init__(self, server, api_key, directory, model, expected_thinking=None):
        self.server = server.rstrip("/")
        self.api_key = api_key
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.expected_thinking = expected_thinking
        self.token = "benchmark_" + uuid4().hex
        self.rows = []
        self.lock = threading.Lock()
        self.responses = set()
        self.httpd = None
        self.phase = ""
        self.deadline = float("inf")
        self.terminal_error = threading.Event()
        self.attempts = {}
        self.last_finished = None

    def __enter__(self):
        recorder = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def fail(self, status, message):
                raw = json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(raw)

            def do_GET(self):
                self.fail(404, "Only benchmark chat completions are available")

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    return self.fail(404, "Unsupported benchmark endpoint")
                if self.headers.get("Authorization") != "Bearer " + recorder.token:
                    return self.fail(401, "Invalid local benchmark token")
                if recorder.terminal_error.is_set():
                    append_json(recorder.directory / "requests.jsonl", {
                        "request_id": uuid4().hex, "phase": recorder.phase, "at": utc(), "model": recorder.model,
                        "event": "blocked_after_error", "http_status": 409,
                        "error": "BENCHMARK_STOPPED_AFTER_FIRST_ERROR"})
                    return self.fail(409, "Benchmark stopped after the first upstream error")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 20*1024*1024:
                        return self.fail(413, "Invalid request size")
                    raw = self.rfile.read(size)
                    body = json.loads(raw)
                    if body.get("model") != recorder.model:
                        return self.fail(400, "Unexpected benchmark model")
                except (ValueError, OSError):
                    return self.fail(400, "Invalid benchmark request")
                recorder.forward(self, body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.httpd.server_port) + "/v1"
        return self

    def cancel(self):
        with self.lock:
            responses = list(self.responses)
        for response in responses:
            try:
                response.close()
            except Exception:
                pass

    def __exit__(self, *exc):
        self.cancel()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def forward(self, handler, body):
        request_id = uuid4().hex
        start = time.monotonic()
        # Request usage metadata without changing generation or server sampling options.
        if body.get("stream"):
            body = {**body, "stream_options": {**body.get("stream_options", {}), "include_usage": True}}
        row = {"request_id": request_id, "phase": self.phase, "at": utc(), "model": self.model,
               "requested_thinking": body.get("think"), "expected_thinking": self.expected_thinking,
               "request_sha256": sha(json.dumps(body, sort_keys=True, ensure_ascii=False)),
               "requested_output": body.get("max_tokens", body.get("max_completion_tokens"))}
        with self.lock:
            identity = (self.phase, row["request_sha256"])
            self.attempts[identity] = self.attempts.get(identity, 0) + 1
            row["identical_request_attempt"] = self.attempts[identity]
            row["idle_gap_seconds"] = round(start-self.last_finished, 4) if self.last_finished else None
        stem = self.directory / request_id
        write_json(stem.with_suffix(".request.json"),
                   json.loads(redact(json.dumps(body, ensure_ascii=False), (self.api_key, self.token))))
        append_json(self.directory / "requests.jsonl", {**row, "event": "started"})
        facts, sent_headers, response = StreamFacts(), False, None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            if self.expected_thinking is not None and (body.get("think") != self.expected_thinking
                    or type(body.get("think")) is not type(self.expected_thinking)):
                facts.error = {"code": "BENCHMARK_THINKING_MISMATCH",
                               "message": "OpenCode请求的think与本题预定模式不同，已停止，未发送到模型。"}
                row["http_status"] = 400
                self.terminal_error.set()
                handler.fail(400, facts.error["message"])
                sent_headers = True
                return
            timeout = max(.1, min(3600, self.deadline-start))
            with httpx.Client(trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(timeout, connect=min(10, timeout))) as client:
                with client.stream("POST", self.server + "/v1/chat/completions", json=body,
                                   headers={"Authorization": "Bearer " + self.api_key}) as response:
                    with self.lock:
                        self.responses.add(response)
                    row["http_status"] = response.status_code
                    row["server_trace_id"] = response.headers.get("x-trace-id")
                    row["headers_seconds"] = round(time.monotonic()-start, 4)
                    handler.send_response(response.status_code)
                    handler.send_header("Content-Type", response.headers.get("content-type", "text/event-stream"))
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    handler.close_connection = True
                    sent_headers = True
                    error_body = ""
                    with redacted_text(stem.with_suffix(".response.sse"), (self.api_key, self.token)) as output:
                        for chunk in response.iter_bytes():
                            if time.monotonic() > self.deadline:
                                raise TimeoutError()
                            text = decoder.decode(chunk)
                            # Responses only contain synthetic task data. No auth headers are logged.
                            output.write(text)
                            if response.status_code != 200 and len(error_body) < 65536:
                                error_body += text
                            facts.feed(text, time.monotonic()-start)
                            if facts.error:
                                self.terminal_error.set()
                            handler.wfile.write(chunk)
                            handler.wfile.flush()
                    if error_body:
                        try:
                            facts.error = json.loads(error_body).get("error", error_body)
                        except ValueError:
                            facts.error = error_body
                        self.terminal_error.set()
                    if response.status_code == 200 and body.get("stream") and not facts.done and not facts.error:
                        facts.error = {"code": "BENCHMARK_INCOMPLETE_STREAM", "message": "Upstream stream ended without DONE"}
                        self.terminal_error.set()
                    if (response.status_code == 200 and body.get("stream") and not facts.error
                            and not facts.text.strip() and not any(t["name"] for t in facts.tools.values())):
                        facts.error = {"code": "BENCHMARK_EMPTY_COMPLETION", "message": "No text or tool calls in completion"}
                        self.terminal_error.set()
        except (httpx.HTTPError, OSError, TimeoutError, RuntimeError) as error:
            row["transport_error"] = type(error).__name__
            row["deadline_exceeded"] = time.monotonic() >= self.deadline
            self.terminal_error.set()
            if not sent_headers:
                try:
                    handler.fail(502, "Upstream benchmark transport failed")
                except OSError:
                    pass
        finally:
            if response is not None:
                with self.lock:
                    self.responses.discard(response)
            row.update(seconds=round(time.monotonic()-start, 4), ended_at=utc(), job_id=facts.job_id, usage=facts.usage,
                       tool_calls=list(facts.tools.values()),
                       first_content_seconds=facts.first_content, first_text_seconds=facts.first_text,
                       first_reasoning_seconds=facts.first_reasoning, reasoning_chars=facts.reasoning_chars,
                       finish_reason=facts.finish, done=facts.done,
                       error=redact(json.dumps(facts.error, ensure_ascii=False), (self.api_key, self.token)) if facts.error else None)
            if facts.text:
                stem.with_suffix(".answer.txt").write_text(redact(facts.text, (self.api_key, self.token)), encoding="utf-8")
            with self.lock:
                self.last_finished = time.monotonic()
                self.rows.append(row)
                append_json(self.directory / "requests.jsonl", {**row, "event": "finished"})


def completion(url, token, body, timeout):
    """A standalone no-history API probe through the same live recorder."""
    facts = StreamFacts()
    start = time.monotonic()
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(timeout, connect=10)) as client:
        with client.stream("POST", url + "/chat/completions", headers={"Authorization": "Bearer " + token},
                           json=body) as response:
            if response.status_code != 200:
                response.read()
                try:
                    error = response.json().get("error", "HTTP " + str(response.status_code))
                except (ValueError, AttributeError):
                    error = "HTTP " + str(response.status_code)
                return {"http_status": response.status_code, "answer": "", "error": error}
            for text in response.iter_text():
                if time.monotonic()-start > timeout:
                    raise TimeoutError()
                facts.feed(text, time.monotonic()-start)
    return {"http_status": response.status_code, "answer": facts.text, "usage": facts.usage,
            "reasoning_chars": facts.reasoning_chars, "first_reasoning_seconds": facts.first_reasoning,
            "done": facts.done, "error": facts.error, "finish_reason": facts.finish}


def wait_ready(server, api_key, seconds=60):
    """Let cancellation/recovery finish before starting another scored case."""
    end = time.monotonic() + seconds
    with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client:
        while time.monotonic() < end:
            try:
                response = client.get(server + "/api/v1/ready", headers={"Authorization": "Bearer " + api_key})
                if response.status_code == 401:
                    raise ValueError("独立API key已失效，请重新设置后续跑。")
                if response.status_code == 200 and response.json().get("canAcceptJobs"):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)
    raise ValueError("5080尚未恢复就绪，剩余题目保留未开始状态，可稍后--resume续跑。")

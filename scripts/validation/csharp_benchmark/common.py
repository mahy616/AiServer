"""Small durable artifact and process helpers shared by both benchmark machines."""
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
VERSION = "csharp-scripts-v4-roles"
MODELS = {
    "7b": "aiserver-qwen25-coder:7b-instruct-q5_k_m-yarn128k",
    "14b": "aiserver-qwen25-coder:14b-instruct-q4_k_m-yarn128k",
    "9b": "qwen3.5:9b-q4_K_M",
    "qwen38": "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS",
}
TIMEOUTS = {"easy": 180, "medium": 600, "hard": 1800}
THINKING_TIMEOUTS = {"easy": 600, "medium": 1800, "hard": 3600}
ROLE_LEVELS = {"7b": ("easy",), "14b": ("easy",), "9b": ("easy", "medium"), "qwen38": ("medium", "hard")}
THINKING_MODELS = frozenset(("9b", "qwen38"))
ACTIVE_MODELS = ("9b", "qwen38")  # Older aliases remain readable for archived reports.


def condition_plan(aliases, thinking):
    if thinking not in ("off", "on", "both", "fallback"):
        raise ValueError("thinking须为off、on或both。")
    conditions = {}
    for alias in dict.fromkeys(aliases):
        if alias not in MODELS:
            raise ValueError("包含已移除或未知模型，请新建运行目录。")
        if thinking != "on":
            conditions[alias] = {"base_alias": alias, "thinking": False}
        if thinking != "off" and alias in THINKING_MODELS:
            conditions[alias + "-thinking"] = {"base_alias": alias, "thinking": True}
    if thinking in ("on", "both", "fallback") and not any(alias in THINKING_MODELS for alias in aliases):
        raise ValueError("所选模型没有thinking测试项；请选择9b或qwen38，或使用--thinking off。")
    if not conditions:
        raise ValueError("所选模型没有thinking测试项；请选择9b或qwen38，或使用--thinking off。")
    return conditions


def utc():
    return datetime.now(timezone.utc).isoformat()


def redact(value, secrets=()):
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    return re.sub(r"ais_[a-f0-9]{32}_[A-Za-z0-9_-]+", "[redacted]", value)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_json(path, value):
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()


@contextmanager
def redacted_text(path, secrets=()):
    """Preserve line-oriented streams while redacting keys split across chunks."""
    with Path(path).open("w", encoding="utf-8") as stream:
        class Writer:
            pending = ""

            def write(self, text):
                self.pending += text
                split = self.pending.rfind("\n") + 1
                if not split and len(self.pending) > 65536:
                    split = len(self.pending)-4096
                    for secret in secrets:
                        at = self.pending.rfind(secret, 0, split+len(secret)) if secret else -1
                        if at >= 0 and at < split < at+len(secret):
                            split = at
                if split:
                    stream.write(redact(self.pending[:split], secrets))
                    stream.flush()
                    self.pending = self.pending[split:]

            def finish(self):
                stream.write(redact(self.pending, secrets))
                stream.flush()
        writer = Writer()
        try:
            yield writer
        finally:
            writer.finish()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        job = getattr(process, "_csharp_job", None)
        if job:
            job.terminate()
        else:
            process.kill()
    else:
        import signal
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def run_process(command, cwd, env, timeout, prefix, secrets=(), limit=8*1024*1024, stop_event=None):
    """Drain both pipes live, redact secrets and bound disk use, including on timeout."""
    prefix = Path(prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
                               start_new_session=os.name != "nt")
    job = None
    if os.name == "nt":
        # Reuse the project's kernel-owned tree and resume helpers; avoid taskkill races.
        sys.path.insert(0, str(ROOT / "server/src"))
        try:
            from aiserver.operations.engine import WindowsJob
            from aiserver.operations.processes import resume_suspended
            job = WindowsJob(process)
            process._csharp_job = job
            resume_suspended(process)
        except BaseException:
            if job:
                job.close()
            else:
                process.kill()
            process.wait(timeout=15)
            process.stdout.close()
            process.stderr.close()
            raise
    def drain(pipe, suffix):
        remaining, pending = limit, b""
        with prefix.with_suffix(suffix).open("wb") as output:
            while True:
                chunk = pipe.read1(16384)
                if not chunk:
                    break
                pending += chunk
                # Retain an unfinished line so tokens/UTF-8 cannot straddle writes.
                split = pending.rfind(b"\n") + 1
                if not split and len(pending) > 2*1024*1024:
                    split = len(pending) - 4096
                    for secret in secrets:
                        encoded = secret.encode()
                        at = pending.rfind(encoded, 0, split + len(encoded)) if encoded else -1
                        if at >= 0 and at < split < at + len(encoded):
                            split = at
                if split:
                    clean = redact(pending[:split].decode("utf-8", "replace"), secrets).encode("utf-8")
                    output.write(clean[:max(0, remaining)])
                    output.flush()
                    remaining -= len(clean)
                    pending = pending[split:]
            clean = redact(pending.decode("utf-8", "replace"), secrets).encode("utf-8")
            output.write(clean[:max(0, remaining)])
            if remaining < len(clean):
                output.write(b"\n[artifact truncated]\n")
        pipe.close()
    threads = [threading.Thread(target=drain, args=(process.stdout, ".jsonl")),
               threading.Thread(target=drain, args=(process.stderr, ".stderr.txt"))]
    for thread in threads:
        thread.start()
    timed_out, aborted = False, False
    try:
        deadline = start + max(.01, timeout)
        while process.poll() is None:
            if stop_event is not None and stop_event.is_set():
                aborted = True
                stop_process(process)
                break
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                process.wait(timeout=min(.1, max(.01, deadline-time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_process(process)
    except BaseException:
        stop_process(process)
        raise
    finally:
        if job:
            job.close()
        for thread in threads:
            thread.join(timeout=20)
    return {"exit_code": process.returncode, "timed_out": timed_out, "aborted": aborted,
            "seconds": round(time.monotonic()-start, 3)}


def read_events(path):
    result = []
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except ValueError:
                pass
    return result

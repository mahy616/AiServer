"""5080只读实时采集；不生成报告，不改服务配置／数据库，不启动或停止模型。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4
import zipfile

from csharp_benchmark.common import ROOT, append_json, redact, utc, write_json

INFERENCE_FIELDS = ("Model", "ProfileId", "EngineVersion", "ExpectedQuantization", "KvCacheType", "FlashAttention",
                    "NumCtx", "NumPredict", "ReservedTokens", "NumBatch", "RequireFullGpu", "Temperature", "TopP", "TopK",
                    "Seed", "Think", "PresencePenalty", "NumParallel", "MaxLoadedModels", "MaxOutputTokens", "GenerationDefaults")


def select_config(explicit):
    if explicit:
        return explicit.resolve()
    preferences = ROOT / ".local/server-manager/launcher.json"
    if preferences.is_file():
        saved = json.loads(preferences.read_text(encoding="utf-8-sig"))
        if saved.get("config"):
            path = Path(saved["config"])
            return path if path.is_absolute() else ROOT / path
    path = ROOT / "config/server.local.json"
    if not path.is_file():
        raise ValueError("找不到实际服务配置，请用--server-config指定5080正在使用的JSON。")
    return path


def resolve(path):
    path = Path(path)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def configuration(path):
    server = json.loads(path.read_text(encoding="utf-8-sig"))
    result = {"at": utc(), "server_config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "port": server.get("port", 18080), "provider": server.get("provider"),
              "execution_timeout_seconds": server.get("execution_timeout_seconds", 600),
              "manage_engine": server.get("manage_engine", True), "models": []}
    for item in [server.get("inference_config", "config/inference.coder.example.json"),
                 *server.get("additional_inference_configs", [])]:
        cfg = json.loads(resolve(item).read_text(encoding="utf-8-sig"))
        result["models"].append({**{k: cfg[k] for k in INFERENCE_FIELDS if k in cfg},
                                  "inference_config_sha256": hashlib.sha256(resolve(item).read_bytes()).hexdigest(),
                                  "tokenizer_sha256": cfg.get("Budget", {}).get("TokenizerSha256"),
                                  "budget_method": cfg.get("Budget", {}).get("Method")})
    return server, result


class LogTail:
    """Short-lived readers permit Windows log rotation; follow only named log files."""
    def __init__(self, path, output):
        self.path, self.output = path, output
        self.positions, self.pending = {}, {}
        for candidate in self.paths():
            if candidate.is_file():
                stat = candidate.stat()
                identity = (stat.st_dev, stat.st_ino)
                self.positions[identity] = max(0, stat.st_size-65536) if candidate == path else stat.st_size

    def paths(self):
        return [self.path.with_name(self.path.name + "." + str(i)) for i in range(5, 0, -1)] + [self.path]

    def poll(self):
        for candidate in self.paths():
            try:
                with candidate.open("rb") as stream:
                    stat = candidate.stat()
                    identity = (stat.st_dev, stat.st_ino)
                    offset = self.positions.get(identity, 0)
                    stream.seek(offset if offset <= stat.st_size else 0)
                    pending = self.pending.pop(identity, b"")
                    while chunk := stream.read(65536):
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            text = line.decode("utf-8", "replace").rstrip()
                            if "env=" in text and "server config" in text:
                                text = "[engine environment dump omitted]"
                            append_json(self.output, {"at": utc(), "line": redact(text)})
                        if len(pending) > 1024*1024:
                            append_json(self.output, {"at": utc(), "line": "[oversized log line omitted]"})
                            pending = b""
                    self.positions[identity] = stream.tell()
                    self.pending[identity] = pending
            except FileNotFoundError:
                pass

    def close(self):
        self.poll()
        for pending in self.pending.values():
            if pending:
                append_json(self.output, {"at": utc(), "line": redact(pending.decode("utf-8", "replace"))})


def task_rows(database, since, user=None):
    # No Store(), migrations, whole-database copy, request bodies or credential tables.
    if not database.is_file():
        return []
    with closing(sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True, timeout=1)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        where, values = "p.created_at_ms>=?", [since]
        if user:
            where += " AND p.user_id=?"
            values.append(user)
        return [dict(row) for row in connection.execute("SELECT p.*,j.profile_id FROM task_performance p "
                "LEFT JOIN jobs j USING(job_id) WHERE " + where + " ORDER BY p.created_at_ms,p.job_id", values)]


def archive(output):
    target = Path(str(output) + ".zip")
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as packed:
        for path in sorted(output.iterdir()):
            if path.is_file():
                packed.write(path, path.name)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--output", type=Path, help="新建采集目录")
    parser.add_argument("--label", default="", help="可填客户端运行标识，便于后续人工关联")
    parser.add_argument("--user", help="仅采集该账号任务指标；共享硬件和进程日志仍为整机数据")
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--duration", type=float, default=0, help="采集秒数；默认直到Ctrl+C")
    args = parser.parse_args()
    if args.interval < .2 or args.duration < 0:
        parser.error("interval至少0.2秒，duration不能为负")
    config_path = select_config(args.server_config)
    server, cfg = configuration(config_path)
    data = resolve(server.get("data_directory", ".local/server"))
    database = data / "jobs.db"
    if not database.is_file():
        raise ValueError("实际配置的数据目录中没有jobs.db；请先启动AiServer并核对--server-config。")
    try:
        task_rows(database, time.time_ns()//1000000, args.user)
    except sqlite3.Error:
        raise ValueError("任务监控表不可读；请确认实际服务配置和当前AiServer版本。") from None
    sys.path.insert(0, str(ROOT / "server/src"))
    from aiserver.operations.monitoring import HostSampler
    output = (args.output or ROOT / ".local/csharp-benchmark-server" /
              (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])).resolve()
    output.mkdir(parents=True, exist_ok=False)
    start_ms, start = time.time_ns()//1000000, time.monotonic()
    write_json(output / "configuration.json", {**cfg, "label": args.label, "user_filter": args.user,
                                             "capture_started_at": utc(), "start_ms": start_ms,
                                             "note": "配置值不等同于引擎实际生效证明；请结合engine日志核对KV和GPU驻留。"})
    sampler, seen = HostSampler(), {}
    tails = [LogTail(data / "logs" / name, output / (name + ".jsonl")) for name in ("service.log", "engine.log")]
    print("采集目录：" + str(output) + "；保持AiServer运行。结束时按Ctrl+C，仅停止采集器。", flush=True)
    try:
        while not args.duration or time.monotonic()-start < args.duration:
            tick = time.monotonic()
            append_json(output / "resources.jsonl", {"at": utc(), "at_ms": time.time_ns()//1000000, **sampler.sample()})
            snapshot_path = data / "monitor-state.json"
            if snapshot_path.is_file():
                try:
                    snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
                    append_json(output / "monitor.jsonl", {k: snap.get(k) for k in
                                ("observed_at_ms", "pid", "scheduler", "local_ready", "waiting_tasks", "active_tasks")})
                except (OSError, ValueError):
                    pass
            try:
                for row in task_rows(database, start_ms, args.user):
                    encoded = json.dumps(row, sort_keys=True)
                    if seen.get(row["job_id"]) != encoded:
                        append_json(output / "tasks.jsonl", {"captured_at": utc(), **row})
                        seen[row["job_id"]] = encoded
            except (sqlite3.Error, OSError) as error:
                append_json(output / "collector-errors.jsonl", {"at": utc(), "error": type(error).__name__})
            for tail in tails:
                try:
                    tail.poll()
                except OSError as error:
                    append_json(output / "collector-errors.jsonl", {"at": utc(), "error": type(error).__name__})
            time.sleep(max(.01, args.interval-(time.monotonic()-tick)))
    except KeyboardInterrupt:
        pass
    finally:
        for tail in tails:
            try:
                tail.close()
            except OSError:
                pass
        try:
            for row in task_rows(database, start_ms, args.user):
                if seen.get(row["job_id"]) != json.dumps(row, sort_keys=True):
                    append_json(output / "tasks.jsonl", {"captured_at": utc(), **row})
                    seen[row["job_id"]] = json.dumps(row, sort_keys=True)
        except (sqlite3.Error, OSError):
            pass
        try:
            _, final_cfg = configuration(config_path)
            write_json(output / "configuration-end.json", final_cfg)
        except (OSError, ValueError):
            pass
        write_json(output / "capture.json", {"label": args.label, "finished_at": utc(), "seconds": time.monotonic()-start,
                                            "task_count": len(seen), "database_found": database.is_file()})
    print("服务端原始采集包：" + str(archive(output)), flush=True)
    print("未生成服务端报告；将采集包提供后再单独分析。", flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print("采集启动失败：" + str(error), file=sys.stderr)
        raise SystemExit(1)

"""Local-only configuration/backup helpers. Stdlib only; usable before upgrades."""
from contextlib import contextmanager, closing
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PYTHON = r"C:\ProgramData\anaconda3\envs\trt_test_1013\python.exe"
EDITABLE = {"manage_natapp", "natapp_executable", "natapp_config",
            "payload_days", "max_request_bytes", "public_base_url"}


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("配置有重复字段，请先检查原文件。")
        value[key] = item
    return value


def read_config(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("配置含无效数字。")))
    if not isinstance(value, dict):
        raise ValueError("服务配置必须是JSON对象。")
    return value, data


def resolve(root, value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (Path(root) / path).resolve()


def data_directory(root, config):
    return resolve(root, config.get("data_directory", ".local/server"))


def load_lock(root):
    # Load the same stdlib-only lock even when the old package lacks new dependencies.
    spec = importlib.util.spec_from_file_location("manager_instance_lock", Path(root) / "server/src/aiserver/operations/lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.InstanceLock


@contextmanager
def offline(root, config):
    lock = load_lock(root)(data_directory(root, config)).acquire()
    try:
        yield
    finally:
        lock.release()


def inspect_config(root, path):
    value, original = read_config(path)
    def existing(options, fallback):
        return next((name for name in options if resolve(root, name).is_file()), fallback)
    fields = {
        "manage_natapp": value.get("manage_natapp", value.get("environment") == "Production"),
        "natapp_executable": value.get("natapp_executable", existing(
            (".local/natapp/natapp.exe", "natapp.exe"), ".local/natapp/natapp.exe")),
        "natapp_config": value.get("natapp_config", existing(
            (".local/natapp/config.ini", "natapp.ini", "config.ini"), ".local/natapp/config.ini")),
        "payload_days": value.get("payload_days", 1),
        "max_request_bytes": value.get("max_request_bytes", 20971520),
        "public_base_url": value.get("public_base_url", "") or "",
    }
    return {"fields":fields, "sha256":hashlib.sha256(original).hexdigest(),
            "summary":{"port":value.get("port", 18080), "provider":value.get("provider", "Ollama"), "dataDirectory":str(data_directory(root,value)),
                       "inferenceConfig":value.get("inference_config", "config/inference.qwen35.example.json"),
                       "allowedHosts":value.get("allowed_hosts", [])}}


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_config(root, path, fields, expected_hash):
    if set(fields) != EDITABLE or type(fields["manage_natapp"]) is not bool:
        raise ValueError("配置字段无效，请重新加载。")
    spec = importlib.util.spec_from_file_location("manager_urls",Path(root)/"server/src/aiserver/operations/urls.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    fields = {**fields,"public_base_url":module.public_url(fields["public_base_url"])}
    for key, minimum, maximum in (("payload_days",1,30),
                                  ("max_request_bytes",1,20971520)):
        if type(fields[key]) is not int or not minimum <= fields[key] <= maximum:
            raise ValueError("保留天数或请求大小超出允许范围。")
    for key in ("natapp_executable", "natapp_config"):
        if not isinstance(fields[key],str) or not fields[key].strip():
            raise ValueError("请选择NATAPP程序和INI路径。")
        if fields["manage_natapp"] and not resolve(root, fields[key]).is_file():
            raise ValueError("NATAPP程序或INI文件不存在，请在本机重新选择。")
    original, _ = read_config(path)
    with offline(root, original):
        value, before = read_config(path)
        if hashlib.sha256(before).hexdigest() != expected_hash:
            raise ValueError("配置已被其他操作修改，请重新加载后再保存。")
        if fields["payload_days"] > value.get("tombstone_days",30):
            raise ValueError("正文保留天数不能超过原有幂等记录保留天数。")
        backup = Path(path).with_name(Path(path).name + ".before-manager-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6] + ".bak")
        with backup.open("xb") as stream:
            stream.write(before)
        value.update(fields)
        atomic_write(path, (json.dumps(value,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
    return {"backup":str(backup), **inspect_config(root,path)}


def backup_database(root, config_path):
    config, _ = read_config(config_path)
    source = data_directory(root,config) / "jobs.db"
    if not source.is_file():
        raise ValueError("尚无业务数据库，无需备份；不会创建空库。")
    with offline(root,config):
        destination = Path(root) / ".local/backups" / ("before-update-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6] + ".db")
        destination.parent.mkdir(parents=True,exist_ok=True)
        try:
            with closing(sqlite3.connect(source.as_uri()+"?mode=ro",uri=True)) as old:
                if old.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("数据库完整性检查失败，未创建备份。")
                version = old.execute("PRAGMA user_version").fetchone()[0]
                with closing(sqlite3.connect(destination)) as new:
                    old.backup(new)
                    if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("备份完整性检查失败。")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return {"backup":str(destination),"schemaVersion":version}


def child_environment(root):
    environment = {key:value for key,value in os.environ.items() if not key.upper().startswith("AISERVER_")}
    environment.update({"PYTHONPATH":str(Path(root)/"server/src"), "PYTHONUTF8":"1",
                        "PYTHONIOENCODING":"utf-8", "PYTHONUNBUFFERED":"1"})
    return environment

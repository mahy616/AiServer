"""Import an already downloaded Ollama 8B model and register it beside the primary.

Run with the API stopped. Uses the project's existing engine and dependencies;
never downloads, generates text, changes 4B files, or terminates external apps.
"""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
MODEL = "qwen3-vl:8b"
MANIFEST = Path("manifests/registry.ollama.ai/library/qwen3-vl/8b")


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


support = module("install_manager_support", ROOT / "scripts/server/manager_support.py")
provision = module("install_model_preparation", Path(__file__).with_name("Prepare-AdditionalModel.py"))


def log(message):
    print(message, flush=True)


def resolve(root, path):
    return support.resolve(root, path)


def read(path):
    return support.read_config(path)[0]


def snapshot(paths):
    return {path: path.read_bytes() for path in paths}


def unchanged(before):
    if any(not path.is_file() or path.read_bytes() != data for path, data in before.items()):
        raise ValueError("配置或身份锁已被其他操作修改，已停止；请重新运行。")


def select_config(root, explicit=None):
    if explicit:
        return resolve(root, explicit)
    prefs = root / ".local/server-manager/launcher.json"
    if prefs.is_file():
        saved = read(prefs).get("config")
        if saved:
            path = resolve(root, saved)
            if not path.is_file():
                raise ValueError("管理器记住的服务配置不存在，请用 --server-config 指定实际配置。")
            return path
    path = root / "config/server.local.json"
    if not path.is_file():
        raise ValueError("没有找到私有服务配置，请用 --server-config 指定；不会修改公开模板。")
    return path


def manifest_info(store):
    path = store / MANIFEST
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("8B模型清单过大。")
    value = read(path)
    if value.get("schemaVersion") != 2 or not isinstance(value.get("layers"), list):
        raise ValueError("8B模型清单格式无效。")
    blobs = {}
    for item in [value.get("config"), *value["layers"]]:
        if (not isinstance(item, dict) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("digest", ""))
                or type(item.get("size")) is not int or item["size"] <= 0):
            raise ValueError("8B模型清单的文件摘要或大小无效。")
        name = item["digest"].replace(":", "-", 1)
        if name in blobs and blobs[name] != item["size"]:
            raise ValueError("8B模型清单含冲突的文件大小。")
        blobs[name] = item["size"]
    if not any(item.get("mediaType") == "application/vnd.ollama.image.projector" for item in value["layers"]):
        raise ValueError("8B模型清单缺少视觉投影器。")
    return raw, blobs


def source_candidates(root, primary):
    values = [os.environ.get("OLLAMA_MODELS")]
    if os.name == "nt":
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    values.append(winreg.QueryValueEx(handle, "OLLAMA_MODELS")[0])
            except OSError:
                pass
    values += [str(Path.home() / ".ollama/models"), primary["ModelsPath"]]
    return [resolve(root, os.path.expandvars(str(value)).strip().strip('"')) for value in values if value]


def find_source(root, primary, explicit=None):
    candidates = [resolve(root, explicit)] if explicit else source_candidates(root, primary)
    found = {}
    for store in dict.fromkeys(candidates):
        if (store / MANIFEST).is_file():
            raw, blobs = manifest_info(store)
            found[store] = (raw, blobs)
    # The running download app can reveal a custom location even when this shell
    # inherited older environment variables. Query loopback only; no disk scan.
    if not found and not explicit:
        for base_url in dict.fromkeys([primary["BaseUrl"], "http://127.0.0.1:11434"]):
            try:
                show = provision.LocalOllama(base_url)("/api/show", {"model": MODEL})
                match = re.search(r"(?mi)^\s*FROM\s+(.+?)\s*$", show.get("modelfile", ""))
                if match:
                    blob = Path(match[1].strip().strip('"')).resolve()
                    store = blob.parent.parent
                    if blob.parent.name == "blobs" and (store / MANIFEST).is_file():
                        found[store] = manifest_info(store)
            except (OSError, ValueError):
                pass
    if not found:
        raise ValueError("找不到已下载的 qwen3-vl:8b。请用 --source-models 指定包含 blobs 和 manifests 的目录。")
    if len({hashlib.sha256(item[0]).hexdigest() for item in found.values()}) != 1:
        raise ValueError("发现不同版本的8B模型，请用 --source-models 明确选择目录：" + "；".join(map(str, found)))
    store = next(iter(found))
    return store, *found[store]


def verify_blob(path, size):
    if not path.is_file() or path.stat().st_size != size:
        raise ValueError("模型文件缺失或大小不符：" + str(path))
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if path.name != "sha256-" + digest:
        raise ValueError("模型文件SHA256不符，保留原文件，请检查：" + str(path))


def publish(path, data):
    """Publish a new small file completely, without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        install_new(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_new(temporary, path):
    if os.name == "nt":
        os.rename(temporary, path)  # Windows refuses an existing destination, including on exFAT.
    else:
        os.link(temporary, path)


def copy_model(source, target, raw, blobs):
    target.mkdir(parents=True, exist_ok=True)
    destination_manifest = target / MANIFEST
    if destination_manifest.exists() and destination_manifest.read_bytes() != raw:
        raise ValueError("工程已有不同的8B清单，已停止，不覆盖：" + str(destination_manifest))
    missing = sum(size for name, size in blobs.items() if not (target / "blobs" / name).exists())
    if shutil.disk_usage(target).free < missing + 16 * 1024 * 1024:
        raise ValueError("工程模型目录所在磁盘空间不足。")
    for index, (name, size) in enumerate(blobs.items(), 1):
        origin, destination = source / "blobs" / name, target / "blobs" / name
        if not origin.resolve().is_relative_to(source) or not destination.resolve().is_relative_to(target):
            raise ValueError("模型文件链接指向模型库之外，已停止。")
        log(f"[2/5] 文件 {index}/{len(blobs)}，{size / 1024**2:.1f} MiB：复制/校验 {name[:19]}")
        if destination.exists():
            verify_blob(destination, size)
            continue
        if not origin.is_file() or origin.stat().st_size != size:
            raise ValueError("下载目录中的模型文件不完整：" + str(origin))
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=destination.parent)
        try:
            digest, count = hashlib.sha256(), 0
            with origin.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                while block := incoming.read(4 * 1024 * 1024):
                    count += len(block)
                    if count > size:
                        raise ValueError("复制过程中源模型大小发生变化。")
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if count != size or name != "sha256-" + digest.hexdigest():
                raise ValueError("源模型SHA256或大小不符：" + str(origin))
            install_new(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if not destination_manifest.exists():
        publish(destination_manifest, raw)
        return destination_manifest
    return None


@contextmanager
def offline(root, server):
    sys.path.insert(0, str(root / "server/src"))
    from aiserver.operations.lock import InstanceLock
    try:
        data_lock = InstanceLock(resolve(root, server.get("data_directory", ".local/server"))).acquire()
    except RuntimeError:
        raise ValueError("AiServer仍在运行，请先从管理器/原控制台停止服务，再运行本脚本。") from None
    try:
        engine_lock = InstanceLock(root / ".local/managed-engine").acquire()
        try:
            yield
        finally:
            engine_lock.release()
    finally:
        data_lock.release()


@contextmanager
def temporary_engine(root, primary, work):
    from aiserver.operations.engine import ManagedEngine
    cfg = dict(primary)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        cfg["BaseUrl"] = f"http://127.0.0.1:{probe.getsockname()[1]}"
    engine = ManagedEngine(cfg, work / "logs")
    # Dedicated process record: never reclaim a regular server's engine record.
    engine.lock_directory = work / "engine"
    engine.record = engine.lock_directory / "process.json"
    previous_no_prune = os.environ.get("OLLAMA_NOPRUNE")
    try:
        os.environ["OLLAMA_NOPRUNE"] = "1"
        asyncio.run(engine.start())
        yield provision.LocalOllama(cfg["BaseUrl"], generate_timeout=cfg.get("GenerateTimeoutSeconds", 600))
    finally:
        try:
            asyncio.run(engine.close())
        finally:
            if previous_no_prune is None:
                os.environ.pop("OLLAMA_NOPRUNE", None)
            else:
                os.environ["OLLAMA_NOPRUNE"] = previous_no_prune


def verify_profiles(profiles, fetch):
    version, tags = fetch("/api/version"), fetch("/api/tags")
    for cfg, lock in profiles:
        entry = next((item for item in tags.get("models", []) if item.get("name") == cfg["Model"]), None)
        show = fetch("/api/show", {"model": cfg["Model"]})
        if (version.get("version") != cfg["EngineVersion"] or not entry
                or entry.get("digest") != lock["model"]["digest"]
                or show.get("details", {}).get("quantization_level") != cfg["ExpectedQuantization"]
                or not set(cfg["RequiredCapabilities"]).issubset(show.get("capabilities", []))
                or hashlib.sha256(show.get("template", "").encode("utf-8")).hexdigest() != lock["template_sha256"]):
            raise ValueError("原有模型身份校验失败，保留服务配置：" + cfg["Model"])


def install(root=ROOT, *, server_config=None, source_models=None, engine_factory=temporary_engine):
    root = Path(root).resolve()
    server_path = select_config(root, server_config)
    server, original = support.read_config(server_path)
    if server.get("provider") != "Ollama" or not server.get("manage_engine", True):
        raise ValueError("本工具要求已有 provider=Ollama、manage_engine=true 的工程部署。")
    settings_source = (root / "server/src/aiserver/settings.py").read_text(encoding="utf-8-sig")
    if "additional_inference_configs" not in settings_source or not (root / "server/src/aiserver/inference/registry.py").is_file():
        raise ValueError("服务端尚不支持多模型，请先更新0.6.0或以上完整服务端程序。")
    paths = [server_path, root / provision.TEMPLATE]
    profiles = []
    for path in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        cfg_path = resolve(root, path)
        cfg = read(cfg_path)
        lock_path = resolve(root, cfg["ModelLockPath"])
        profiles.append((cfg, read(lock_path)))
        paths.extend([cfg_path, lock_path])
    primary = profiles[0][0]
    if primary["Model"] == MODEL:
        raise ValueError("主模型已经是8B；本工具用于保留原主模型并追加8B。")
    provision.LocalOllama(primary["BaseUrl"])  # Validate loopback before launching anything.
    if not resolve(root, primary["ExecutablePath"]).is_file():
        raise ValueError("工程原有Ollama程序不存在，请检查主推理配置的ExecutablePath。")
    for cfg, _ in profiles:
        if any(cfg[key] != primary[key] for key in provision.PROCESS_KEYS):
            raise ValueError("原有模型没有共享同一引擎配置，请先检查。")
    before = snapshot(paths)
    if before[server_path] != original or any(
            read(paths[2 + index * 2]) != cfg or read(paths[3 + index * 2]) != lock
            for index, (cfg, lock) in enumerate(profiles)):
        raise ValueError("读取期间配置或身份锁已改变，请重新运行。")
    protected = set(paths[:4])
    source, raw, blobs = find_source(root, primary, source_models)
    target = resolve(root, primary["ModelsPath"])
    log("[1/5] 服务配置：" + str(server_path))
    log("下载模型目录：" + str(source))
    log("工程模型目录：" + str(target))
    work = root / ".local/model-install" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    created, new_manifest = [], None
    with offline(root, server):
        unchanged(before)
        try:
            new_manifest = copy_model(source, target, raw, blobs)
            log("[3/5] 临时启动工程引擎，检查原有模型和8B的真实元信息（不生成回答）。")
            with engine_factory(root, primary, work) as fetch:
                verify_profiles(profiles, fetch)
                outputs, result = provision.plan(root, server_path, model=MODEL, fetch=fetch)
                actual_digest = hashlib.sha256(raw).hexdigest()
                if result["digest"] != actual_digest:
                    raise ValueError("引擎返回的8B摘要与复制清单不一致。")
            unchanged(before)
            # Recheck every generated destination before committing any config.
            for path, value, identity in outputs:
                if path in protected:
                    raise ValueError("生成路径与原有配置/身份锁冲突。")
                if path.exists() and provision.comparable(read(path), identity) != provision.comparable(value, identity):
                    raise ValueError("已有8B配置不同，保留原文件，请先检查：" + str(path))
            patch = outputs[-1][1]
            updated = {**server, **patch}
            backup = None
            if updated != server:
                backup = server_path.with_name(server_path.name + ".before-8b-" + work.name + ".bak")
                publish(backup, original)
            log("[4/5] 写入独立8B配置和身份锁，备份并合并服务配置。")
            for path, value, _ in outputs:
                if not path.exists():
                    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    publish(path, data)
                    created.append((path, data))
            unchanged(before)
            if updated != server:
                support.atomic_write(server_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except BaseException:
            # Only remove this run's unchanged additions. Verified content-addressed
            # blobs stay available for retry; original 4B/config files are untouched.
            for path, data in reversed(created):
                if path.is_file() and path.read_bytes() == data:
                    path.unlink()
            if new_manifest and new_manifest.is_file() and new_manifest.read_bytes() == raw:
                new_manifest.unlink()
            raise
    log("[5/5] 完成：原主模型保留，已注册 " + MODEL)
    if backup:
        log("原服务配置备份：" + str(backup))
    log("请退出用于下载的托盘Ollama，然后从原管理器/入口启动服务，刷新客户端模型列表。")
    return {**result, "serverConfig": str(server_path), "backup": str(backup) if backup else None,
            "sourceModels": str(source), "targetModels": str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--source-models", type=Path)
    args = parser.parse_args()
    try:
        install(server_config=args.server_config, source_models=args.source_models)
        return 0
    except KeyboardInterrupt:
        log("操作已取消；已生效的原服务配置保留。")
        return 1
    except Exception as error:
        # Never print private JSON values, response bodies or command lines.
        message = str(error) if type(error) is ValueError else "请检查配置、模型目录、工程依赖和引擎（" + type(error).__name__ + "）。"
        log("部署未完成：" + message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

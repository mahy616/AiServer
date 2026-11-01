"""Download fixed Ollama manifests and register additional models transactionally."""
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location("pinned_model_dependencies", Path(__file__).with_name("Install-Qwen25Coder7B.py"))
dependencies = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dependencies)
legacy = dependencies.legacy
from coder_support import download, digest

MODEL_KEYS = ("qwen38", "gptoss", "gemma12", "gemma26")


def log(message):
    print(message, flush=True)


def manifest_info(source):
    raw = source["manifest"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != source["manifest_sha256"]:
        raise ValueError("公开模型清单SHA256不符。")
    value = json.loads(raw)
    if value.get("schemaVersion") != 2 or not isinstance(value.get("layers"), list):
        raise ValueError("公开模型清单格式无效。")
    items = {}
    for item in [value.get("config"), *value["layers"]]:
        if (not isinstance(item, dict) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item.get("digest", ""))
                or type(item.get("size")) is not int or item["size"] <= 0):
            raise ValueError("公开模型清单含无效blob摘要／大小。")
        sha = item["digest"][7:]
        if sha in items and items[sha] != item["size"]:
            raise ValueError("公开模型清单含冲突的blob大小。")
        items[sha] = item["size"]
    if sum(item.get("mediaType") == "application/vnd.ollama.image.model" for item in value["layers"]) != 1:
        raise ValueError("公开模型清单必须包含一个模型权重。")
    for sha, data in source.get("inline_blobs", {}).items():
        if (not isinstance(data, str) or sha not in {"sha256:" + key for key in items}
                or "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest() != sha
                or len(data.encode("utf-8")) != items[sha[7:]]):
            raise ValueError("内嵌模型元数据的SHA256／大小不符。")
    return raw, items


def download_model(root, cfg, source, source_models=None, *, allow_download=True):
    raw, items = manifest_info(source)
    target = legacy.resolve(root, cfg["ModelsPath"])
    relative = Path(source["manifest_path"])
    path = (target / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(target / "manifests"):
        raise ValueError("模型清单路径必须位于目标模型库的manifests内。")
    if path.exists() and path.read_bytes() != raw:
        raise ValueError("目标模型库已有不同版本的清单，拒绝覆盖：" + str(path))
    target.mkdir(parents=True, exist_ok=True)
    missing = 0
    for sha, size in items.items():
        destination = target / "blobs" / ("sha256-" + sha)
        partial = destination.with_suffix(".download")
        if not destination.exists():
            missing += max(0, size - (partial.stat().st_size if partial.is_file() and not source_models else 0))
    if shutil.disk_usage(target).free < missing + 64 * 1024**2:
        raise ValueError(f"模型库磁盘空间不足，尚需约{missing / 1024**3:.2f}GiB及下载余量。")
    for index, (sha, size) in enumerate(items.items(), 1):
        destination = target / "blobs" / ("sha256-" + sha)
        if not destination.resolve().is_relative_to(target):
            raise ValueError("blob链接指向模型库之外，已停止。")
        log(f"文件 {index}/{len(items)}：{size / 1024**2:.1f}MiB，下载或校验复用。")
        if not destination.exists() and source_models:
            source_root = legacy.resolve(root, source_models)
            origin = source_root / "blobs" / destination.name
            if not origin.resolve().is_relative_to(source_root):
                raise ValueError("源blob链接指向源模型库之外。")
            if origin.is_file():
                legacy.verify_blob(origin, size)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".copy-" + uuid4().hex)
                try:
                    shutil.copyfile(origin, temporary)
                    if temporary.stat().st_size != size or digest(temporary) != sha:
                        raise ValueError("复制后模型blob的SHA256／大小不符。")
                    legacy.install_new(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
        if destination.exists():
            legacy.verify_blob(destination, size)
        elif "sha256:" + sha in source.get("inline_blobs", {}):
            legacy.publish(destination, source["inline_blobs"]["sha256:" + sha].encode("utf-8"))
        elif allow_download:
            partial = destination.with_suffix(".download")
            if not partial.resolve().is_relative_to(target):
                raise ValueError("下载断点链接指向模型库之外。")
            if partial.is_file() and partial.stat().st_size >= size:
                if partial.stat().st_size != size or digest(partial) != sha:
                    raise ValueError("下载断点的SHA256／大小不符，请检查：" + str(partial))
                legacy.install_new(partial, destination)
            else:
                download(source["registry"] + "/blobs/sha256:" + sha, destination, sha, size)
        else:
            raise ValueError("缺少固定版本blob；移除--no-download允许下载，或指定--source-models。")
    if not path.exists():
        legacy.publish(path, raw)
    return path


def install(key, root=ROOT, *, server_config=None, context=None, batch_size=None,
            output_tokens=None,
            source_models=None, prepare_only=False, allow_download=True,
            engine_factory=legacy.temporary_engine):
    if key not in MODEL_KEYS:
        raise ValueError("未知安装目标。")
    from aiserver.inference.configuration import PROCESS_KEYS, runtime_options, validate_options
    from aiserver.context.window_budget import validate_window
    from aiserver.inference.gpu import require_gpu_status
    from aiserver.inference.thinking import verify_thinking

    root = Path(root).resolve()
    source_path = root / f"config/models/{key}.source.json"
    template_path = root / f"config/inference.{key}.example.json"
    config_path = root / f"config/inference.{key}.local.json"
    source, source_raw = legacy.support.read_config(source_path)
    manifest_info(source)
    server_path = legacy.select_config(root, server_config)
    server, original = legacy.support.read_config(server_path)
    if server.get("provider") != "Ollama" or not server.get("manage_engine", True):
        raise ValueError("需要provider=Ollama、manage_engine=true的现有部署。")
    paths = [legacy.resolve(root, path) for path in [server["inference_config"],
             *server.get("additional_inference_configs", [])]]
    profiles = []
    for path in paths:
        existing = legacy.read(path)
        profiles.append((existing, legacy.read(legacy.resolve(root, existing["ModelLockPath"]))))
    primary = profiles[0][0]
    if primary["EngineVersion"] != source["engine_version"]:
        raise ValueError("本入口锁定Ollama " + source["engine_version"] + "，请检查原工程引擎；不自动更换引擎。")
    if not legacy.resolve(root, primary["ExecutablePath"]).is_file():
        raise ValueError("工程原有Ollama程序不存在。")
    legacy.provision.LocalOllama(primary["BaseUrl"])
    template, template_raw = legacy.support.read_config(template_path)
    cfg = {**template, **{key: primary[key] for key in PROCESS_KEYS}}
    if config_path.exists():
        cfg = legacy.read(config_path)
        if ((context is not None and cfg["NumCtx"] != context)
                or (batch_size is not None and cfg["NumBatch"] != batch_size)
                or (output_tokens is not None and cfg.get("MaxOutputTokens", cfg.get("NumPredict")) != output_tokens)):
            raise ValueError("已有配置调优不同；请停服修改实际推理JSON，不自动覆盖。")
    else:
        if context is not None:
            cfg["NumCtx"] = context
        if batch_size is not None:
            cfg["NumBatch"] = batch_size
        if output_tokens is not None:
            cfg["NumPredict"] = output_tokens
        cfg["ProfileId"] = f"{key}-{cfg['NumCtx']}-v1"
    identity_path = legacy.resolve(root, cfg["ModelLockPath"])
    if (cfg["Model"] != source["model"] or cfg["ExpectedQuantization"] != source["quantization"]
            or cfg["RequiredCapabilities"] != template["RequiredCapabilities"]
            or cfg["ModelLockPath"] != template["ModelLockPath"]
            or cfg.get("ThinkingLevels") != template.get("ThinkingLevels")
            or cfg.get("RequireFullGpu") is not True or cfg["ImagePolicy"] != template["ImagePolicy"]):
        raise ValueError("模型配置的身份、能力、图片策略或全GPU要求与固定来源不符。")
    if not 8192 <= cfg["NumCtx"] <= source["native_context"]:
        raise ValueError("上下文须至少8192且不超过固定模型原生窗口。")
    validate_options(cfg)
    validate_window(cfg)
    if any(cfg[k] != primary[k] for k in PROCESS_KEYS):
        raise ValueError("新模型须使用原工程同版本引擎、端口和模型库。")
    if any(cfg[k] != 1 for k in ("MaxLoadedModels", "NumParallel", "EngineMaxQueue")):
        raise ValueError("本工程要求单生成位。")
    before = {server_path: original, source_path: source_raw, template_path: template_raw}
    ids = set()
    for path, (existing, lock) in zip(paths, profiles):
        validate_options(existing)
        validate_window(existing)
        if any(existing[k] != primary[k] for k in PROCESS_KEYS):
            raise ValueError("原有模型没有共享同一引擎配置。")
        if existing["Model"] in ids or (existing["Model"] == cfg["Model"] and path != config_path):
            raise ValueError("不同推理配置重复注册同一模型。")
        ids.add(existing["Model"])
        lock_path = legacy.resolve(root, existing["ModelLockPath"])
        before[path] = path.read_bytes()
        before[lock_path] = lock_path.read_bytes()
        if (json.loads(before[path].decode("utf-8-sig")) != existing
                or json.loads(before[lock_path].decode("utf-8-sig")) != lock):
            raise ValueError("读取期间配置或身份锁已改变，请重试。")
    if identity_path in paths or identity_path in (server_path, source_path, template_path, config_path):
        raise ValueError("新模型身份锁路径与其他配置冲突。")
    if any(identity_path == legacy.resolve(root, item["ModelLockPath"]) and path != config_path
           for path, (item, _) in zip(paths, profiles)):
        raise ValueError("新模型身份锁与其他模型冲突。")
    for path in (config_path, identity_path):
        if path.exists() and path not in before:
            before[path] = path.read_bytes()
    if config_path in before and json.loads(before[config_path].decode("utf-8-sig")) != cfg:
        raise ValueError("读取期间模型配置已改变。")
    if config_path.exists() and not identity_path.is_file():
        raise ValueError("已有模型配置缺少身份锁，请先检查。")
    additional = list(server.get("additional_inference_configs", []))
    if config_path not in paths:
        additional.append(config_path.relative_to(root).as_posix())
    if len(additional) > 7:
        raise ValueError("最多支持一个主模型和七个附加模型。")
    updated = {**server, "additional_inference_configs": additional,
               "execution_timeout_seconds": max(server.get("execution_timeout_seconds", 600), 3600)}
    operation = root / f".local/{key}/install" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    created, backup = [], None
    with legacy.offline(root, server):
        legacy.unchanged(before)
        try:
            log("[1/3] 下载／校验固定版本：" + cfg["Model"])
            download_model(root, cfg, source, source_models, allow_download=allow_download)
            legacy.unchanged(before)
            log(f"[2/3] 以{cfg['NumCtx']}窗口、{cfg.get('KvCacheType', 'f16')} KV检查身份和全GPU驻留。")
            log("本次引擎日志：" + str(operation / "logs/engine.log"))
            with engine_factory(root, cfg, operation) as fetch:
                legacy.verify_profiles(profiles, fetch)
                tags = fetch("/api/tags").get("models", [])
                entry = next((item for item in tags if item.get("name") == cfg["Model"]), None)
                show = fetch("/api/show", {"model": cfg["Model"]})
                if (fetch("/api/version").get("version") != cfg["EngineVersion"] or not entry
                        or entry.get("digest") != source["manifest_sha256"]
                        or show.get("details", {}).get("quantization_level") != source["quantization"]
                        or show.get("model_info", {}).get("general.architecture") != source["architecture"]
                        or not set(source["required_capabilities"]).issubset(show.get("capabilities", []))
                        or not isinstance(show.get("template"), str) or not show["template"]):
                    raise ValueError("模型的实际摘要、架构、量化、模板或能力不符；保留原服务配置。")
                verify_thinking(cfg, show)
                loaded = fetch("/api/generate", {"model": cfg["Model"], "prompt": "", "stream": False,
                    "truncate": False, "shift": False,
                    "keep_alive": cfg["KeepAlive"], "options": runtime_options(cfg)})
                if loaded.get("done") is not True or loaded.get("error"):
                    raise ValueError("完整窗口加载失败；保留原服务配置。")
                row = require_gpu_status(cfg, fetch("/api/ps"))
                log(f"全GPU预检通过：窗口{row['context_length']}，引擎报告{row['size_vram']/1024**3:.2f}GiB。")
                identity = {"engine_version": cfg["EngineVersion"], "model": entry,
                    "template_sha256": hashlib.sha256(show["template"].encode("utf-8")).hexdigest(),
                    "capabilities": show["capabilities"], "source_manifest_sha256": source["manifest_sha256"],
                    "image_policy": cfg["ImagePolicy"]}
            if identity_path.exists():
                previous_identity = legacy.read(identity_path)
                comparable_identity = dict(identity)
                # Older target-host locks did not record this optional provenance.
                # The actual engine digest has already matched the pinned source.
                if "source_manifest_sha256" not in previous_identity:
                    comparable_identity.pop("source_manifest_sha256")
                if legacy.provision.comparable(previous_identity, True) != legacy.provision.comparable(comparable_identity, True):
                    raise ValueError("已有模型身份锁不同，拒绝覆盖。")
            legacy.unchanged(before)
            for path, value in ((config_path, cfg), (identity_path, identity)):
                if not path.exists():
                    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    legacy.publish(path, raw)
                    created.append((path, raw))
            if not prepare_only and updated != server:
                backup = server_path.with_name(server_path.name + f".before-{key}-" + operation.name + ".bak")
                legacy.publish(backup, original)
                legacy.unchanged(before)
                legacy.support.atomic_write(server_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except BaseException:
            for path, raw in reversed(created):
                if path.is_file() and path.read_bytes() == raw:
                    path.unlink()
            raise
    log("[3/3] 完成：" + cfg["Model"] + ("；仅准备，服务配置未改。" if prepare_only else "；保留主模型，已追加注册。"))
    if backup:
        log("服务配置备份：" + str(backup))
    return {"model": cfg["Model"], "config": str(config_path), "lock": str(identity_path),
            "serverConfig": str(server_path), "backup": str(backup) if backup else None}


def main(default_models=("qwen38",)):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--models", nargs="+", choices=MODEL_KEYS, default=default_models)
    parser.add_argument("--source-models", type=Path, help="复用此Ollama模型库中匹配固定摘要的blob")
    parser.add_argument("--context", type=int, help="首次配置窗口；已有调优须停服修改实际JSON")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--output-tokens", type=int, help="首次配置最大输出；思考与正文共用此预算")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--install-dependencies", action="store_true")
    args = parser.parse_args()
    failures = []
    try:
        log("使用Python：" + sys.executable)
        dependencies.ensure_dependencies(root=ROOT, repair=args.install_dependencies, server_config=args.server_config)
        for key in dict.fromkeys(args.models):
            try:
                install(key, server_config=args.server_config, context=args.context, batch_size=args.batch_size,
                        output_tokens=args.output_tokens,
                        source_models=args.source_models, prepare_only=args.prepare_only, allow_download=not args.no_download)
            except Exception as error:
                failures.append(key)
                log(key + "准备失败：" + (str(error) if type(error) is ValueError else "请检查下载网络、依赖和引擎日志（" + type(error).__name__ + "）。"))
        if failures:
            log("未完成：" + "、".join(failures) + "。成功的模型已保留，失败模型的原有注册保持不变；修复后可重试。")
            return 1
        log("全部完成。请从原管理器启动AiServer，再重启OpenCode并用/models选择。")
        return 0
    except KeyboardInterrupt:
        log("已取消；此前成功注册的模型保留，当前模型不切换服务配置。")
    except Exception as error:
        log("安装未完成：" + (str(error) if type(error) is ValueError else type(error).__name__))
    return 1

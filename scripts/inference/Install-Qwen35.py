"""Download the pinned official Qwen3.5-9B Q4_K_M and append it to this service."""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location("qwen35_install_shared", Path(__file__).with_name("Install-Qwen25Coder7B.py"))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
legacy = shared.legacy
from coder_support import download, digest
from model_registration import registration
from qwen35_budget import prepare_tokenizer

CONFIG = "config/inference.qwen35.local.json"
TEMPLATE = "config/inference.qwen35.example.json"
SOURCE = "config/models/qwen35.source.json"
MANIFEST = Path("manifests/registry.ollama.ai/library/qwen3.5/9b-q4_K_M")


def download_model(root, cfg, lock, source_models=None):
    raw = lock["manifest"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != lock["manifest_sha256"]:
        raise ValueError("公开模型清单摘要不符。")
    manifest = json.loads(raw)
    target = legacy.resolve(root, cfg["ModelsPath"])
    path = target / MANIFEST
    if path.exists() and path.read_bytes() != raw:
        raise ValueError("工程已有不同版本的Qwen3.5清单，不自动覆盖。")
    items = [manifest["config"], *manifest["layers"]]
    target.mkdir(parents=True, exist_ok=True)
    missing = sum(item["size"] for item in items if not (target / "blobs" / item["digest"].replace(":", "-", 1)).exists())
    if shutil.disk_usage(target).free < missing + 64 * 1024**2:
        raise ValueError("模型磁盘空间不足；首次需要约6.6GB及下载余量。")
    for index, item in enumerate(items, 1):
        sha = item["digest"].removeprefix("sha256:")
        name = "sha256-" + sha
        destination = target / "blobs" / name
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("模型blob摘要无效。")
        print(f"文件 {index}/{len(items)}：{item['size']/1024**2:.1f} MiB；下载或校验复用。", flush=True)
        if not destination.exists() and source_models:
            origin = legacy.resolve(root, source_models) / "blobs" / name
            if origin.is_file():
                legacy.verify_blob(origin, item["size"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(name + ".copy-" + uuid4().hex)
                try:
                    shutil.copyfile(origin, temporary)
                    if temporary.stat().st_size != item["size"] or digest(temporary) != sha:
                        raise ValueError("复制后模型摘要不符。")
                    legacy.install_new(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
        download(lock["registry"] + "/blobs/" + item["digest"], destination, sha, item["size"])
    if not path.exists():
        legacy.publish(path, raw)
        return path, raw
    return None, raw


def install(root=ROOT, *, server_config=None, context=None, batch_size=None, source_models=None,
            prepare_only=False, engine_factory=legacy.temporary_engine):
    root = Path(root).resolve()
    server_path = legacy.select_config(root, server_config)
    server, original = legacy.support.read_config(server_path)
    if server.get("provider") != "Ollama" or not server.get("manage_engine", True):
        raise ValueError("需要provider=Ollama、manage_engine=true的现有部署。")
    primary_path = legacy.resolve(root, server["inference_config"])
    primary = legacy.read(primary_path)
    source = legacy.read(root / SOURCE)
    if primary["EngineVersion"] != source["engine_version"]:
        raise ValueError("本入口锁定Ollama " + source["engine_version"] + "；不自动更换引擎。")
    cfg = legacy.read(root / TEMPLATE)
    for key in legacy.provision.PROCESS_KEYS:
        cfg[key] = primary[key]
    config_path = root / CONFIG
    identity_path = legacy.resolve(root, cfg["ModelLockPath"])
    if config_path.exists():
        cfg = legacy.read(config_path)
        identity_path = legacy.resolve(root, cfg["ModelLockPath"])
        if cfg["Model"] != source["model"] or not identity_path.is_file():
            raise ValueError("已有Qwen3.5配置或身份锁不完整。")
        if (context is not None and cfg["NumCtx"] != context) or (batch_size is not None and cfg["NumBatch"] != batch_size):
            raise ValueError("已有配置调优不同；请修改实际推理JSON，不自动覆盖。")
    else:
        if context is not None:
            cfg["NumCtx"] = context
        if batch_size is not None:
            cfg["NumBatch"] = batch_size
        cfg["ProfileId"] = f"qwen35-9b-q4-q8kv-{cfg['NumCtx']}-v1"
    cfg["RequireFullGpu"] = True
    if cfg["EngineVersion"] != source["engine_version"] or cfg["ExpectedQuantization"] != source["quantization"]:
        raise ValueError("已有模型配置的引擎版本或量化与固定来源不符。")
    if not 16384 <= cfg["NumCtx"] <= source["native_context"]:
        raise ValueError("上下文须在16384至262144之间，且必须通过全GPU检查。")
    updated, before = registration(root, server, config_path, cfg)
    before[server_path] = original
    before[root / SOURCE] = (root / SOURCE).read_bytes()
    operation = root / ".local/qwen35/install" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    created, manifest_path, backup = [], None, None
    with legacy.offline(root, server):
        legacy.unchanged(before)
        try:
            print("[1/3] 准备官方固定摘要Qwen3.5-9B Q4_K_M；首次约6.6GB。", flush=True)
            manifest_path, manifest_raw = download_model(root, cfg, source, source_models)
            budget = prepare_tokenizer(root, source)
            if config_path.exists() and any(cfg.get("Budget", {}).get(k) != v for k, v in budget.items()):
                raise ValueError("已有9B计数配置需要升级；请先停服运行Repair-InferenceRuntime.py。")
            cfg["Budget"] = {**cfg.get("Budget", {}), **budget}
            print("[2/3] 检查模型身份、工具能力和完整窗口的全GPU驻留。", flush=True)
            print("本次引擎日志：" + str(operation / "logs/engine.log"), flush=True)
            with engine_factory(root, cfg, operation) as fetch:
                entry = next((m for m in fetch("/api/tags").get("models", []) if m["name"] == cfg["Model"]), None)
                show = fetch("/api/show", {"model": cfg["Model"]})
                if (fetch("/api/version").get("version") != cfg["EngineVersion"] or not entry
                        or entry.get("digest") != source["manifest_sha256"]
                        or show.get("details", {}).get("quantization_level") != source["quantization"]
                        or show.get("model_info", {}).get("general.architecture") != source["architecture"]
                        or not {"completion", "tools", "thinking"}.issubset(show.get("capabilities", []))):
                    raise ValueError("实际Qwen3.5的摘要、架构、量化或能力不符。")
                from aiserver.inference.configuration import runtime_options
                from aiserver.inference.gpu import require_gpu_status
                loaded = fetch("/api/generate", {"model": cfg["Model"], "prompt": "", "stream": False,
                    "truncate": False, "shift": False,
                    "keep_alive": cfg["KeepAlive"], "options": runtime_options(cfg)})
                if loaded.get("done") is not True or loaded.get("error"):
                    raise ValueError("完整窗口加载失败，原服务配置保留。")
                require_gpu_status(cfg, fetch("/api/ps"))
                identity = {"engine_version": cfg["EngineVersion"], "model": entry,
                    "template_sha256": hashlib.sha256(show["template"].encode()).hexdigest(),
                    "capabilities": show["capabilities"], "source_manifest_sha256": source["manifest_sha256"],
                    "image_policy": cfg["ImagePolicy"]}
            if identity_path.exists() and legacy.provision.comparable(legacy.read(identity_path), True) != legacy.provision.comparable(identity, True):
                raise ValueError("已有模型身份锁不同，拒绝覆盖。")
            legacy.unchanged(before)
            for path, value in ((config_path, cfg), (identity_path, identity)):
                if not path.exists():
                    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
                    legacy.publish(path, raw)
                    created.append((path, raw))
            if not prepare_only and updated != server:
                backup = server_path.with_name(server_path.name + ".before-qwen35-" + operation.name + ".bak")
                legacy.publish(backup, original)
                legacy.unchanged(before)
                legacy.support.atomic_write(server_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode())
        except BaseException:
            for path, raw in reversed(created):
                if path.is_file() and path.read_bytes() == raw:
                    path.unlink()
            if manifest_path and manifest_path.is_file() and manifest_path.read_bytes() == manifest_raw:
                manifest_path.unlink()
            raise
    print("[3/3] Qwen3.5准备完成。" + ("服务配置未修改。" if prepare_only else "已注册并保留原有模型。"), flush=True)
    if backup:
        print("服务配置备份：" + str(backup), flush=True)
    return {"model": cfg["Model"], "config": str(config_path), "backup": str(backup) if backup else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--source-models", type=Path, help="Reuse exact pinned blobs from this Ollama model store")
    parser.add_argument("--context", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--install-dependencies", action="store_true")
    args = parser.parse_args()
    try:
        shared.ensure_dependencies(repair=args.install_dependencies, server_config=args.server_config)
        install(server_config=args.server_config, source_models=args.source_models, context=args.context,
                batch_size=args.batch_size, prepare_only=args.prepare_only)
        return 0
    except KeyboardInterrupt:
        print("已取消；原配置保留，下载断点可复用。")
    except Exception as error:
        print("Qwen3.5准备失败：" + (str(error) if isinstance(error, ValueError) else type(error).__name__))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

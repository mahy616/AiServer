"""Prepare a pinned Q5 Coder/YaRN model, then atomically switch a stopped service.

Default downloads ~5.5GB; --source-gguf can reuse the pinned source file. Old
models/configurations remain on disk. Only the selected service JSON is merged.
"""
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))
from coder_support import source_files, make_yarn, digest, YARN, compatible_template, TOOL_TEMPLATE_VERSION
from model_registration import registration


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


legacy = module("coder_install_shared", Path(__file__).with_name("Install-Qwen3VL8B.py"))
support = legacy.support
read = legacy.read
publish = legacy.publish
MODEL = "aiserver-qwen25-coder:7b-instruct-q5_k_m-yarn128k"
VARIANTS = {
    "7b": {"model": MODEL, "template": "config/inference.coder.example.json",
           "config": "config/inference.coder.local.json", "directory": ".local/coder",
           "quantization": "Q5_K_M", "layers": 28, "download": "5.5GB"},
    "14b": {"model": "aiserver-qwen25-coder:14b-instruct-q4_k_m-yarn128k",
            "template": "config/inference.coder14b.example.json",
            "config": "config/inference.coder14b.local.json", "directory": ".local/coder14b",
            "quantization": "Q4_K_M", "layers": 48, "download": "9GB"},
}

DEPENDENCY_PROBE = '''import importlib, json
issues = []
for name in ("gguf", "numpy", "tokenizers", "requests", "httpx", "fastapi", "pydantic", "jsonschema", "PIL"):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as error:
        issues.append({"module": name, "missing": error.name, "kind": "ModuleNotFoundError"})
    except Exception as error:
        issues.append({"module": name, "missing": None, "kind": type(error).__name__})
print(json.dumps(issues))
'''


def dependency_command(root):
    return [sys.executable, "-m", "pip", "install", "-r", str(Path(root) / "server/requirements.lock.txt")]


def probe_dependencies():
    # Probe in a fresh process: pip must not replace a NumPy already loaded by this
    # process, and a successful pip exit alone does not prove imports work.
    try:
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", DEPENDENCY_PROBE],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        if result.returncode:
            raise ValueError("依赖检查子进程未能完成；请检查所选Python。")
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        raise ValueError("依赖导入检查未能完成；请检查所选Python环境。") from None


def dependency_summary(issues):
    return "；".join(f'{item["module"]}（缺少 {item["missing"]}）' if item.get("missing")
                    else f'{item["module"]}（{item["kind"]}）' for item in issues)


def ensure_dependencies(root=ROOT, *, repair=False, server_config=None):
    print("所用Python：" + sys.executable, flush=True)
    issues = probe_dependencies()
    if not issues:
        print("依赖导入检查通过。", flush=True)
        return
    command = dependency_command(root)
    description = dependency_summary(issues)
    if not repair:
        raise ValueError("所选Python依赖不完整：" + description + "\n补齐命令："
                         + subprocess.list2cmdline(command))
    print("依赖不完整：" + description, flush=True)
    print("正在使用同一Python按项目锁文件补齐依赖；本步骤不下载模型。", flush=True)
    server_path = legacy.select_config(root, server_config)
    # Updating packages of an active service could leave loaded native modules in
    # an inconsistent state. Use the same service/engine locks as model switching.
    with legacy.offline(root, read(server_path)):
        try:
            result = subprocess.run(command, cwd=root, timeout=600)
        except subprocess.TimeoutExpired:
            raise ValueError("依赖安装超过10分钟；下载模型尚未开始。可单独运行："
                             + subprocess.list2cmdline(command)) from None
        if result.returncode:
            raise ValueError("依赖安装失败，请保留上方pip错误；所用Python：" + sys.executable)
        issues = probe_dependencies()
        if issues:
            raise ValueError("安装后依赖仍无法导入：" + dependency_summary(issues)
                             + "\n请确认所选Python的安装权限与包目录可见性：" + sys.executable)
    print("依赖补齐并重新导入检查通过。", flush=True)


def prepared_blob(root, source, work, primary, source_lock):
    store = support.resolve(root, primary["ModelsPath"])
    record = work.parent / "prepared.json"
    template = json.loads((work / "tokenizer_config.json").read_text(encoding="utf-8"))["chat_template"]
    template_hash = hashlib.sha256(template.encode("utf-8")).hexdigest()
    if record.exists():
        previous = read(record)
        blob = store / "blobs" / ("sha256-" + previous["derived_sha256"])
        if (previous["source_sha256"] == source_lock["blob_sha256"]
                and previous["chat_template_sha256"] == template_hash
                and blob.is_file() and digest(blob) == previous["derived_sha256"]):
            return blob, previous
        raise ValueError("已有YaRN准备记录不一致，请检查：" + str(record))
    destination = store / "blobs" / ("coder-yarn-" + uuid4().hex + ".gguf")
    print("[2/4] 写入YaRN元数据并逐张量校验；不重新量化权重。", flush=True)
    make_yarn(source, destination, chat_template=template, expected_layers=source_lock.get("layers", 28))
    sha = digest(destination)
    blob = destination.with_name("sha256-" + sha)
    if blob.exists():
        if digest(blob) != sha:
            raise ValueError("已有内容寻址blob损坏")
        destination.unlink()
    else:
        os.rename(destination, blob)
    provenance = {"source_sha256": source_lock["blob_sha256"], "derived_sha256": sha,
                  "chat_template_sha256": template_hash, "yarn": YARN}
    publish(record, (json.dumps(provenance, indent=2) + "\n").encode("utf-8"))
    return blob, provenance


def install(root=ROOT, *, server_config=None, source_gguf=None, context=81920,
            prepare_only=False, engine_factory=legacy.temporary_engine, variant="7b",
            batch_size=None, kv_cache=None):
    root = Path(root).resolve()
    selected = VARIANTS[variant]
    model = selected["model"]
    server_path = legacy.select_config(root, server_config)
    server, original = support.read_config(server_path)
    if server.get("provider") != "Ollama" or not server.get("manage_engine", True):
        raise ValueError("需要 provider=Ollama、manage_engine=true 的现有服务配置。")
    primary_path = support.resolve(root, server["inference_config"])
    primary, primary_raw = support.read_config(primary_path)
    if primary["EngineVersion"] != "0.40.9":
        raise ValueError("本工具锁定Ollama 0.40.9，不自动更换引擎版本。")
    cfg = read(root / selected["template"])
    for key in legacy.provision.PROCESS_KEYS:
        cfg[key] = primary[key]
    if context not in (49152, 81920, 131072):
        raise ValueError("不支持的上下文配置。")
    cfg.update(NumCtx=context)
    if batch_size is not None:
        if type(batch_size) is not int or not 1 <= batch_size <= 2048:
            raise ValueError("batch-size必须在1到2048之间。")
        cfg["NumBatch"] = batch_size
    if kv_cache is not None:
        if kv_cache not in ("q4_0", "q8_0"):
            raise ValueError("kv-cache必须为q4_0或q8_0。")
        cfg["KvCacheType"] = kv_cache
    cfg["ProfileId"] = f"qwen25coder{variant}-{selected['quantization'].lower()}-{cfg['KvCacheType']}-{context}-v1"
    config_path = root / selected["config"]
    identity_path = root / cfg["ModelLockPath"]
    if primary_path in (identity_path,) or server_path in (config_path, identity_path):
        raise ValueError("配置路径冲突。")
    # A completed repeated install preserves later capacity/batch tuning.
    if config_path.exists():
        existing = read(config_path)
        if existing["Model"] != model or not identity_path.exists():
            raise ValueError("已有Coder文件不完整或属于不同模型，请先检查。")
        cfg = existing
        if context != 81920 and cfg["NumCtx"] != context:
            raise ValueError("已有Coder容量不同，请按运维说明修改现有配置，不自动覆盖调优。")
        if ((batch_size is not None and cfg["NumBatch"] != batch_size)
                or (kv_cache is not None and cfg["KvCacheType"] != kv_cache)):
            raise ValueError("已有Coder配置不同；请修改实际推理配置，不自动覆盖已有调优。")
    if variant == "14b" and cfg.get("RequireFullGpu") is not True:
        raise ValueError("14B测试配置必须启用RequireFullGpu；请恢复全GPU要求后重试。")
    before = {server_path: original, primary_path: primary_raw}
    sys.path.insert(0, str(root / "server/src"))
    updated, registered_sources = registration(root, server, config_path, cfg)
    before.update(registered_sources)
    for path in (config_path, identity_path):
        if path.exists():
            before[path] = path.read_bytes()
    print(f"[1/4] 准备官方{variant.upper()} {selected['quantization']}和tokenizer（首次约{selected['download']}）。", flush=True)
    source, work, source_lock = source_files(root, source_gguf=source_gguf, variant=variant,
                                           models_path=cfg["ModelsPath"])
    template = compatible_template((work / "ollama.template").read_text(encoding="utf-8"))
    template_sha256 = hashlib.sha256(template.encode("utf-8")).hexdigest()
    # This work directory never shares the normal engine's PID record.
    operation = root / selected["directory"] / "install" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    created = []
    backup = None
    with legacy.offline(root, server):
        legacy.unchanged(before)
        blob, provenance = prepared_blob(root, source, work, cfg, source_lock)
        print("[3/4] 临时启动原引擎，导入并核对真实能力、YaRN及模型身份。", flush=True)
        print("本次引擎日志：" + str(operation / "logs/engine.log"), flush=True)
        with engine_factory(root, cfg, operation) as fetch:
            if fetch("/api/version").get("version") != cfg["EngineVersion"]:
                raise ValueError("实际引擎版本不符。")
            tags = fetch("/api/tags").get("models", [])
            entry = next((m for m in tags if m["name"] == model), None)
            if entry is None:
                fetch("/api/create", {"model": model, "files": {"model.gguf": "sha256:" + provenance["derived_sha256"]},
                                      "template": template,
                                      "license": (work / "LICENSE.txt").read_text(encoding="utf-8"),
                                      "stream": False})
                entry = next((m for m in fetch("/api/tags")["models"] if m["name"] == model), None)
            show = fetch("/api/show", {"model": model})
            if identity_path.exists() and read(identity_path).get("tool_template_version") == "qwen25-json-tools-v1":
                raise ValueError("已有Coder使用旧裸JSON工具模板；请停服运行scripts/inference/Repair-CoderTools.py，再重试安装。")
            origin = re.search(r"(?m)^FROM\s+(.+)$", show.get("modelfile", ""))
            if (not entry or show.get("details", {}).get("quantization_level") != selected["quantization"]
                    or not {"completion", "tools"}.issubset(show.get("capabilities", []))
                    or any(show.get("model_info", {}).get(k) != v for k, v in YARN.items())
                    or hashlib.sha256(show.get("template", "").encode()).hexdigest() != template_sha256
                    or origin is None or Path(origin[1].strip().strip('"')).name != "sha256-" + provenance["derived_sha256"]):
                raise ValueError("实际Coder模型的量化、工具能力、模板或YaRN元数据不符。")
            if cfg.get("RequireFullGpu"):
                from aiserver.inference.configuration import runtime_options
                from aiserver.inference.gpu import require_gpu_status
                print("加载完整窗口并核对GPU驻留；显存不足不会切换服务配置。", flush=True)
                try:
                    loaded = fetch("/api/generate", {"model": model, "prompt": "", "stream": False,
                                                   "keep_alive": cfg["KeepAlive"], "options": runtime_options(cfg)})
                except (OSError, ValueError):
                    raise ValueError("完整窗口加载失败，原服务配置保持不变；请查看上方engine.log。") from None
                if loaded.get("done") is not True or loaded.get("error"):
                    raise ValueError("14B完整窗口加载失败；请查看本次engine.log。")
                try:
                    row = require_gpu_status(cfg, fetch("/api/ps"))
                except (OSError, ValueError):
                    raise ValueError("完整窗口未确认全GPU驻留，已保留原服务配置；请查看本次engine.log。") from None
                print(f"全GPU驻留检查通过：窗口{row['context_length']}，引擎报告{row['size_vram']/1024**3:.2f}GiB。", flush=True)
            identity = {"engine_version": cfg["EngineVersion"], "model": entry,
                        "template_sha256": template_sha256, "tool_template_version": TOOL_TEMPLATE_VERSION,
                        "capabilities": show["capabilities"],
                        "yarn": YARN, "provenance": provenance, "image_policy": cfg["ImagePolicy"]}
        for path, value in ((config_path, cfg), (identity_path, identity)):
            if path.exists() and legacy.provision.comparable(read(path), path == identity_path) != legacy.provision.comparable(value, path == identity_path):
                raise ValueError("已有Coder配置/身份锁不同，拒绝覆盖：" + str(path))
        legacy.unchanged(before)
        try:
            for path, value in ((config_path, cfg), (identity_path, identity)):
                if not path.exists():
                    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    publish(path, data)
                    created.append((path, data))
            if not prepare_only and updated != server:
                backup = server_path.with_name(server_path.name + ".before-coder-" + operation.name + ".bak")
                publish(backup, original)
                legacy.unchanged(before)
                support.atomic_write(server_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except BaseException:
            for path, data in reversed(created):
                if path.is_file() and path.read_bytes() == data:
                    path.unlink()
            raise
    print("[4/4] Coder准备完成。" + ("服务配置未修改。" if prepare_only else "已注册模型并保留原有模型及调优。"), flush=True)
    if backup:
        print("回退备份：" + str(backup), flush=True)
    return {"model": model, "config": str(config_path), "lock": str(identity_path),
            "serverConfig": str(server_path), "backup": str(backup) if backup else None}


def main(variant="7b"):
    parser = argparse.ArgumentParser(description=f"Prepare and switch pinned Qwen2.5-Coder {variant.upper()} with YaRN.")
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--source-gguf", type=Path)
    parser.add_argument("--context", type=int, choices=(49152, 81920, 131072), default=81920)
    parser.add_argument("--batch-size", type=int, help="Initial preparation only; preserve existing tuning")
    parser.add_argument("--kv-cache", choices=("q4_0", "q8_0"), help="Initial preparation only; preserve existing tuning")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--install-dependencies", action="store_true", help="Repair missing dependencies with this Python before preparing models")
    parser.add_argument("--check-dependencies", action="store_true", help="Only check dependencies; do not download models or switch configs")
    args = parser.parse_args()
    try:
        ensure_dependencies(repair=args.install_dependencies, server_config=args.server_config)
        if args.check_dependencies:
            return 0
        install(server_config=args.server_config, source_gguf=args.source_gguf, context=args.context,
                prepare_only=args.prepare_only, variant=variant, batch_size=args.batch_size, kv_cache=args.kv_cache)
        return 0
    except KeyboardInterrupt:
        print("已取消；下载断点保留。", flush=True)
    except Exception as error:
        if isinstance(error, ModuleNotFoundError):
            print("Coder准备失败：缺少Python模块 " + str(error.name) + "\n所用Python：" + sys.executable
                  + "\n补齐命令：" + subprocess.list2cmdline(dependency_command(ROOT)), flush=True)
            return 1
        message = "服务或引擎仍在运行，请先从原入口停止后重试。" if isinstance(error, RuntimeError) and "active instance" in str(error) else None
        print("Coder准备失败：" + (message or (str(error) if type(error) is ValueError else type(error).__name__
                                    + "；请检查网络、空间、依赖和配置。")), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

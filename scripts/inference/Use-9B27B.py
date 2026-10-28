"""停服迁移为9B／27B双模型，备份配置，保留原权重、身份锁、账号与任务。"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server/src"), str(ROOT / "scripts/validation")]
spec = importlib.util.spec_from_file_location("two_model_shared", Path(__file__).with_name("Install-Qwen3VL8B.py"))
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
from aiserver.context.window_budget import validate_window
from aiserver.inference.configuration import PROCESS_KEYS, validate_options
from context_capacity import commit_configs

PROFILES = ("qwen35", "qwen38")


def retire_entries(root):
    manifest = root / "scripts/deployment/coder-retired-files.json"
    entries = json.loads(manifest.read_bytes())
    captured = []
    for entry in entries:
        path = root / entry
        if (Path(entry).is_absolute() or not path.resolve().is_relative_to(root)
                or path.is_symlink() or any(part in (".local", ".git", "..") for part in Path(entry).parts)
                or ".local." in path.name or path.suffix not in (".cmd", ".py", ".json")):
            raise ValueError("旧Coder入口清单包含不允许的路径。")
        if path.exists():
            if not path.is_file():
                raise ValueError("旧Coder入口不是普通文件。")
            captured.append((path, path.read_bytes()))
    if not captured:
        return None
    destination = root / ".local/two-model-upgrade" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    for path, raw in captured:
        backup = destination / path.relative_to(root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb") as stream:
            stream.write(raw)
    for path, raw in captured:
        if path.read_bytes() != raw:
            raise ValueError("旧入口已被其他操作修改，停止清理；双模型注册已生效，可重新执行迁移。")
        path.unlink()
    return destination


def migrate(root=ROOT, server_config=None):
    root = Path(root).resolve()
    server_path = legacy.select_config(root, server_config)
    server, raw = legacy.support.read_config(server_path)
    if server.get("provider") != "Ollama":
        raise ValueError("请选择实际Ollama服务配置。")
    before, templates, registered = {server_path: raw}, {}, {}
    for name in PROFILES:
        path = root / f"config/inference.{name}.example.json"
        template, data = legacy.support.read_config(path)
        before[path] = data
        templates[template["Model"]] = (name, template)
    for entry in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        path = legacy.resolve(root, entry)
        cfg, data = legacy.support.read_config(path)
        before[path] = data
        if cfg.get("Model") not in templates:
            continue
        if cfg["Model"] in registered:
            raise ValueError("同一个保留模型重复注册，请先核对：" + cfg["Model"])
        registered[cfg["Model"]] = (path, cfg)
    selected, changes = [], []
    for model, (name, template) in templates.items():
        if model in registered:
            path, cfg = registered[model]
        else:
            path = root / f"config/inference.{name}.local.json"
            if not path.is_file():
                raise ValueError("请先准备模型后重新迁移：" + model)
            cfg, data = legacy.support.read_config(path)
            before[path] = data
            if cfg.get("Model") != model:
                raise ValueError("已准备配置的模型身份不符：" + str(path))
        if path == server_path or path.name.endswith(".example.json"):
            raise ValueError("保留模型须使用独立私有推理配置：" + str(path))
        lock = legacy.resolve(root, cfg["ModelLockPath"])
        _, data = legacy.support.read_config(lock)
        before[lock] = data
        value = {**cfg, "NumCtx": template["NumCtx"], "NumPredict": template["NumPredict"]}
        if "MaxOutputTokens" in value:
            value["MaxOutputTokens"] = template["NumPredict"]
        validate_options(value)
        validate_window(value)
        selected.append((path, value))
        changes.append((path, value))
    primary = selected[0][1]
    if any(cfg[key] != primary[key] for _, cfg in selected[1:] for key in PROCESS_KEYS):
        raise ValueError("9B与27B须共享同版本引擎、端口和模型库。")
    paths = [p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p) for p, _ in selected]
    updated = {**server, "inference_config": paths[0], "additional_inference_configs": paths[1:]}
    # Publish registration last; a partial configuration write cannot expose it.
    changes.append((server_path, updated))
    with legacy.offline(root, server):
        backups = commit_configs(changes, before, legacy.support, legacy.unchanged, backup_label="two-models")
        print("双模型配置已应用：9B 128K／27B 64K，输出统一16K；主模型为9B。", flush=True)
        for item in backups:
            print(item["config"] + "；备份：" + item["backup"], flush=True)
        retired_backup = retire_entries(root)
    if retired_backup:
        print("旧Coder入口备份：" + str(retired_backup), flush=True)
    print("原模型文件与身份锁保留；启动AiServer并重启OpenCode入口刷新模型列表。", flush=True)
    return backups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    args = parser.parse_args()
    try:
        migrate(server_config=args.server_config)
        return 0
    except KeyboardInterrupt:
        print("已取消；请核对当前服务配置和本次备份后重试。")
    except Exception as error:
        print("双模型迁移失败：" + (str(error) if isinstance(error, (ValueError, FileNotFoundError)) else type(error).__name__))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""停服移除DeepSeek注册、Ollama模型和旧安装入口；保留账号、任务和其他模型。"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from urllib.request import Request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
spec = importlib.util.spec_from_file_location("remove_model_shared", Path(__file__).with_name("Install-Qwen3VL8B.py"))
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

MODELS = (
    "aiserver-deepseek-coder-v2:16b-lite-instruct-q4_0-tools-v1",
    "deepseek-coder-v2:16b-lite-instruct-q4_0",
)
RETIRED_LIST = "scripts/deployment/deepseek-retired-files.json"


def retired_files(root):
    paths = json.loads((root / RETIRED_LIST).read_text(encoding="utf-8"))
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise ValueError("旧入口清单格式无效。")
    result = []
    for relative in paths:
        path = root / relative
        if (not path.resolve().is_relative_to(root) or path.is_symlink()
                or path.resolve() != path.absolute()
                or path.suffix not in (".py", ".cmd", ".json", ".gotmpl")
                or any(part in (".local", ".git", "..") for part in Path(relative).parts)
                or Path(relative).is_absolute() or ".local." in path.name):
            raise ValueError("旧入口清单含不允许删除的路径。")
        if path.exists():
            if not path.is_file():
                raise ValueError("旧入口路径不是普通文件。")
            result.append((path, path.read_bytes()))
    return result


def delete_model(fetch, model):
    # Use Ollama's deletion API so blobs still referenced by other models survive.
    request = Request(fetch.base_url + "/api/delete", method="DELETE",
                      data=json.dumps({"model": model}).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with fetch.opener.open(request, timeout=600) as response:
        response.read(4096)


def remove(root=ROOT, *, server_config=None, engine_factory=legacy.temporary_engine,
           delete=delete_model):
    root = Path(root).resolve()
    path = legacy.select_config(root, server_config)
    server, original = legacy.support.read_config(path)
    if server.get("provider") != "Ollama" or server.get("manage_engine") is not True:
        raise ValueError("需要manage_engine=true的实际Ollama服务配置。")
    before = {path: original}
    remaining, removed = [], []
    for entry in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        target = legacy.resolve(root, entry)
        cfg, raw = legacy.support.read_config(target)
        before[target] = raw
        (removed if cfg.get("Model") in MODELS else remaining).append((entry, cfg))
    if not remaining:
        raise ValueError("移除后没有主模型；请先准备并注册千问模型。")
    primary = remaining[0][1]
    store = legacy.resolve(root, primary["ModelsPath"])
    if any(legacy.resolve(root, cfg["ModelsPath"]) != store for _, cfg in removed):
        raise ValueError("DeepSeek使用不同模型库，请先核对实际推理配置。")
    retired = retired_files(root)
    before.update(retired)
    updated = dict(server)
    if removed:
        updated.update(inference_config=remaining[0][0],
                       additional_inference_configs=[entry for entry, _ in remaining[1:]])
    operation = root / ".local/deepseek-removal" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    report = {"passed": False, "plannedUnregister": [cfg["Model"] for _, cfg in removed], "unregistered": [],
              "deleted": [], "retiredFiles": [], "backup": None}
    with legacy.offline(root, server):
        legacy.unchanged(before)
        operation.mkdir(parents=True)
        try:
            # Unregister before deleting weights: a partial failure can be retried
            # without leaving the service pointing at a deleted model.
            if updated != server:
                backup = path.with_name(path.name + ".before-remove-deepseek-" + operation.name + ".bak")
                legacy.publish(backup, original)
                report["backup"] = str(backup)
                legacy.unchanged(before)
                raw = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                legacy.support.atomic_write(path, raw)
                before[path] = raw
                report["unregistered"] = list(report["plannedUnregister"])
            with engine_factory(root, primary, operation) as fetch:
                if fetch("/api/version").get("version") != primary["EngineVersion"]:
                    raise ValueError("实际Ollama版本与配置不同，未删除模型。")
                tags = {item["name"]: item["digest"] for item in fetch("/api/tags").get("models", [])}
                preserved = {name: digest for name, digest in tags.items() if name not in MODELS}
                for name in MODELS:
                    if name in tags:
                        legacy.unchanged(before)
                        delete(fetch, name)
                        report["deleted"].append(name)
                after = {item["name"]: item["digest"] for item in fetch("/api/tags").get("models", [])}
                if any(name in after for name in MODELS) or after != preserved:
                    raise ValueError("模型删除后列表核对失败，请查看本次摘要。")
            legacy.unchanged(before)
            for target, raw in retired:
                legacy.publish(operation / "retired-source" / target.relative_to(root), raw)
            legacy.unchanged(before)
            for target, _ in retired:
                target.unlink()
                report["retiredFiles"].append(target.relative_to(root).as_posix())
            report["passed"] = True
        except BaseException as error:
            report["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
            raise
        finally:
            (operation / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("清理摘要：" + str(operation / "summary.json"), flush=True)
            if report["backup"]:
                print("服务配置备份：" + report["backup"], flush=True)
    print("DeepSeek注册、模型及旧入口已移除；其他模型和私有字段保留。", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    args = parser.parse_args()
    try:
        remove(server_config=args.server_config)
        return 0
    except Exception as error:
        print("清理未完成：" + (str(error) if isinstance(error, ValueError) else type(error).__name__))
        print("已完成的移除会保留；修复后可重复运行。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

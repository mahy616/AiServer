"""停服应用9B／27B推荐窗口／输出、升级9B计数或调整单模型；保留其他私有字段。"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("runtime_repair_dependencies", Path(__file__).with_name("Install-Qwen35.py"))
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)
legacy = install.legacy
from qwen35_budget import prepare_tokenizer


def set_model_outputs(output, root=ROOT, server_config=None, *, recommended_contexts=False):
    """Update registered active models only; no tokenizer download or engine load."""
    from aiserver.inference.configuration import validate_options
    from aiserver.context.window_budget import validate_window
    if type(output) is not int or output < 1:
        raise ValueError("输出上限须为正整数。")
    root = Path(root).resolve()
    path = legacy.select_config(root, server_config)
    server, original = legacy.support.read_config(path)
    if server.get("provider") != "Ollama":
        raise ValueError("需要选择实际Ollama服务配置。")
    before = {path: original}
    models = {}
    for name in ("qwen35", "qwen38"):
        template = root / f"config/inference.{name}.example.json"
        cfg, raw = legacy.support.read_config(template)
        models[cfg["Model"]] = cfg["NumCtx"]
        before[template] = raw
    selected, seen = [], set()
    for entry in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        target = legacy.resolve(root, entry)
        cfg, raw = legacy.support.read_config(target)
        before[target] = raw
        if cfg.get("Model") not in models:
            continue
        if cfg["Model"] in seen:
            raise ValueError("不同配置重复注册同一个模型：" + cfg["Model"])
        seen.add(cfg["Model"])
        value = {**cfg, "NumPredict": output}
        if recommended_contexts:
            if target.name.endswith(".example.json"):
                raise ValueError("请先把注册的公开推理模板复制为私有配置，并更新服务注册：" + str(target))
            value["NumCtx"] = models[cfg["Model"]]
        if "MaxOutputTokens" in value:
            value["MaxOutputTokens"] = output
        validate_options(value)
        validate_window(value)
        if value != cfg:
            selected.append((target, raw, (json.dumps(value, ensure_ascii=False, indent=2)+"\n").encode("utf-8")))
    if not seen:
        raise ValueError("未注册9B或Qwen3.8，请核对实际服务配置。")
    changed = []
    with legacy.offline(root, server):
        legacy.unchanged(before)
        # Back up every destination before the first replacement.
        for target, raw, updated in selected:
            backup = target.with_name(target.name + (".before-recommended-" if recommended_contexts else ".before-output-") +
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6] + ".bak")
            legacy.publish(backup, raw)
            changed.append({"config": str(target), "backup": str(backup)})
        expected = dict(before)
        written = []
        try:
            for target, raw, updated in selected:
                legacy.unchanged(expected)
                legacy.support.atomic_write(target, updated)
                written.append((target, raw, updated))
                expected[target] = updated
            legacy.unchanged(expected)
        except BaseException:
            for target, raw, updated in reversed(written):
                if target.is_file() and target.read_bytes() == updated:
                    legacy.support.atomic_write(target, raw)
            raise
    print(("已应用双模型推荐窗口和输出上限：" if recommended_contexts else "已注册双模型输出上限：") + str(output) + "；修改" + str(len(changed)) + "份配置。")
    if recommended_contexts:
        for model, context in models.items():
            if model in seen:
                print(model + "：上下文=" + str(context) + "，最大输出=" + str(output))
    for item in changed:
        print(item["config"] + "；回退备份：" + item["backup"])
    print("重启AiServer和OpenCode入口后核对models容量；身份锁和其他调优保留。")
    return changed


def set_qwen38_context(context, root=ROOT, server_config=None, *, output=None):
    """Change only the registered window; Ollama allocates it on next startup."""
    from aiserver.inference.configuration import validate_options
    from aiserver.context.window_budget import validate_window
    root = Path(root).resolve()
    path = legacy.select_config(root, server_config)
    server, original = legacy.support.read_config(path)
    if server.get("provider") != "Ollama":
        raise ValueError("需要选择实际Ollama服务配置。")
    source_path = root / "config/models/qwen38.source.json"
    source, source_raw = legacy.support.read_config(source_path)
    if context is not None and (type(context) is not int or not 8192 <= context <= source["native_context"]):
        raise ValueError("上下文须至少8192且不超过固定模型原生窗口。")
    if output is not None and (type(output) is not int or output < 1):
        raise ValueError("输出上限须为正整数。")
    if context is None and output is None:
        raise ValueError("须指定上下文或输出上限。")
    selected = []
    for entry in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        target = legacy.resolve(root, entry)
        cfg, raw = legacy.support.read_config(target)
        if cfg.get("Model") == source["model"]:
            selected.append((target, cfg, raw))
    if len(selected) != 1:
        raise ValueError("须恰好注册一个Qwen3.8模型，请核对实际服务配置。")
    target, cfg, raw = selected[0]
    value = dict(cfg)
    if context is not None:
        value["NumCtx"] = context
    if output is not None:
        value["NumPredict"] = output
        if "MaxOutputTokens" in value:
            value["MaxOutputTokens"] = output
    validate_options(value)
    validate_window(value)
    before = {path: original, source_path: source_raw, target: raw}
    with legacy.offline(root, server):
        legacy.unchanged(before)
        if value == cfg:
            print("Qwen3.8容量已是指定值，未改配置。")
            return []
        backup = target.with_name(target.name + (".before-output-" if output is not None else ".before-context-") +
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6] + ".bak")
        legacy.publish(backup, raw)
        legacy.unchanged(before)
        legacy.support.atomic_write(target, (json.dumps(value, ensure_ascii=False, indent=2)+"\n").encode("utf-8"))
    print("Qwen3.8窗口／输出上限：" + str(value["NumCtx"]) + "/" +
          str(value.get("MaxOutputTokens", value["NumPredict"])) + "；回退备份：" + str(backup))
    print("下次启动由Ollama实际分配；显存／完整窗口加载失败会报错，不自动降低窗口。")
    return [{"config": str(target), "backup": str(backup)}]


def repair(root=ROOT, server_config=None):
    root = Path(root).resolve()
    path = legacy.select_config(root, server_config)
    server, original = legacy.support.read_config(path)
    if server.get("provider") != "Ollama":
        raise ValueError("需要选择实际Ollama服务配置。")
    source_path = root / "config/models/qwen35.source.json"
    source, source_raw = legacy.support.read_config(source_path)
    selected = []
    for entry in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        target = legacy.resolve(root, entry)
        cfg, raw = legacy.support.read_config(target)
        if cfg.get("Model") == source["model"]:
            selected.append((target, cfg, raw))
    if not selected:
        print("未注册9B；无需计数配置迁移。")
        return []
    if len(selected) != 1:
        raise ValueError("9B重复注册，请先核对模型列表。")
    changed = []
    before = {path: original, source_path: source_raw, **{target: raw for target, _, raw in selected}}
    with legacy.offline(root, server):
        legacy.unchanged(before)
        budget = prepare_tokenizer(root, source)
        legacy.unchanged(before)
        for target, cfg, raw in selected:
            merged_budget = {**cfg.get("Budget", {}), **budget}
            if cfg.get("Budget") == merged_budget:
                print("9B计数资源校验通过，配置已是当前版本。")
                continue
            backup = target.with_name(target.name + ".before-runtime-" +
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6] + ".bak")
            legacy.publish(backup, raw)
            legacy.unchanged(before)
            value = {**cfg, "Budget": merged_budget}
            legacy.support.atomic_write(target, (json.dumps(value, ensure_ascii=False, indent=2)+"\n").encode("utf-8"))
            changed.append({"config": str(target), "backup": str(backup)})
            print("已升级9B完整提示计数；回退备份：" + str(backup))
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--install-dependencies", action="store_true")
    parser.add_argument("--recommended-contexts", action="store_true", help="应用9B／27B推荐窗口和统一16K输出；只修改实际已注册模型")
    parser.add_argument("--models-output", type=int, help="统一实际注册的9B／Qwen3.8输出上限，如16384")
    parser.add_argument("--qwen38-context", type=int, help="只修改已注册Qwen3.8窗口，如65536；下次启动由Ollama分配")
    parser.add_argument("--qwen38-output", type=int, help="修改已注册Qwen3.8输出上限，如16384；保留总窗口与其他配置")
    args = parser.parse_args()
    if args.recommended_contexts and any(value is not None for value in (args.models_output, args.qwen38_context, args.qwen38_output)):
        parser.error("--recommended-contexts不能与其他容量迁移参数同时使用")
    if args.models_output is not None and (args.qwen38_context is not None or args.qwen38_output is not None):
        parser.error("--models-output不能与--qwen38-context／--qwen38-output同时使用")
    try:
        if args.recommended_contexts:
            set_model_outputs(16384, server_config=args.server_config, recommended_contexts=True)
            return 0
        if args.models_output is not None:
            set_model_outputs(args.models_output, server_config=args.server_config)
            return 0
        install.shared.ensure_dependencies(repair=args.install_dependencies, server_config=args.server_config)
        if args.qwen38_context is not None or args.qwen38_output is not None:
            set_qwen38_context(args.qwen38_context, server_config=args.server_config, output=args.qwen38_output)
        else:
            repair(server_config=args.server_config)
        return 0
    except Exception as error:
        print("升级失败：" + (str(error) if isinstance(error, ValueError) else type(error).__name__))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

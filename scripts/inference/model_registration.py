"""Plan model registration without rewriting tuned inference files or private fields."""
import json
from pathlib import Path

KNOWN_CONFIGS = ("config/inference.coder.local.json", "config/inference.coder14b.local.json",
                 "config/inference.qwen35.local.json", "config/inference.qwen38.local.json")


def registration(root, server, selected_path, selected):
    from aiserver.inference.configuration import PROCESS_KEYS, validate_options
    from aiserver.context.window_budget import validate_window
    root = Path(root).resolve()
    selected_path = (root / selected_path).resolve()
    paths = [selected_path, *(root / path for path in [server["inference_config"],
             *server.get("additional_inference_configs", [])]),
             *(root / path for path in KNOWN_CONFIGS if (root / path).is_file())]
    configs, sources, ids = [], {}, set()
    for path in dict.fromkeys(path.resolve() for path in paths):
        raw = path.read_bytes() if path.is_file() else None
        cfg = selected if path == selected_path else json.loads(raw.decode("utf-8-sig")) if raw else None
        if cfg is None:
            raise ValueError("已注册的模型配置缺失：" + str(path))
        if raw is not None and path == selected_path:
            captured = json.loads(raw.decode("utf-8-sig"))
            if {**captured, "RequireFullGpu": True} != {**selected, "RequireFullGpu": True}:
                raise ValueError("读取期间模型配置已改变，请重新运行。")
        validate_options(cfg)
        validate_window(cfg)
        if any(cfg[key] != selected[key] for key in PROCESS_KEYS):
            raise ValueError("模型须共享同版本引擎、端口和模型库；请检查：" + str(path))
        if cfg["Model"] in ids:
            raise ValueError("不同配置重复注册同一个模型：" + cfg["Model"])
        ids.add(cfg["Model"])
        configs.append(path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path))
        if raw is not None:
            sources[path] = raw
        lock = (root / cfg["ModelLockPath"]).resolve()
        if path != selected_path or lock.is_file():
            sources[lock] = lock.read_bytes()
    if len(configs) > 8:
        raise ValueError("最多注册8个模型。")
    return {**server, "inference_config": configs[0], "additional_inference_configs": configs[1:],
            "execution_timeout_seconds": max(server.get("execution_timeout_seconds", 600), 3600),
            "submissions_per_minute": max(server.get("submissions_per_minute", 6), 60)}, sources

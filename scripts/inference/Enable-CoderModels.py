"""Register existing 7B/14B (and prepared 9B) without downloading or changing tuning."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/inference"))
sys.path.insert(0, str(ROOT / "server/src"))
import importlib.util
spec = importlib.util.spec_from_file_location("enable_models_installer", ROOT / "scripts/inference/Install-Qwen3VL8B.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
from model_registration import registration


def enable(root=ROOT, server_config=None, engine_factory=legacy.temporary_engine):
    root = Path(root).resolve()
    server_path = legacy.select_config(root, server_config)
    server, raw = legacy.support.read_config(server_path)
    if server.get("provider") != "Ollama" or not server.get("manage_engine", True):
        raise ValueError("需要provider=Ollama、manage_engine=true。")
    paths = [root / "config/inference.coder.local.json", root / "config/inference.coder14b.local.json"]
    if not all(path.is_file() for path in paths):
        raise ValueError("请先使用7B和14B安装入口准备两个模型。")
    selected = legacy.read(paths[0])
    updated, before = registration(root, server, paths[0], selected)
    before[server_path] = raw
    operation = root / ".local/model-install" / ("enable-" + uuid4().hex)
    backup = None
    with legacy.offline(root, server):
        legacy.unchanged(before)
        # Use each profile's own process-wide KV/FA settings for capacity checks.
        for path in [updated["inference_config"], *updated["additional_inference_configs"]]:
            cfg = legacy.read(root / path)
            lock = legacy.read(root / cfg["ModelLockPath"])
            cfg["RequireFullGpu"] = True
            print("检查模型：" + cfg["Model"], flush=True)
            with engine_factory(root, cfg, operation / uuid4().hex) as fetch:
                legacy.verify_profiles([(cfg, lock)], fetch)
                from aiserver.inference.configuration import runtime_options
                from aiserver.inference.gpu import require_gpu_status
                result = fetch("/api/generate", {"model": cfg["Model"], "prompt": "", "stream": False,
                    "keep_alive": cfg["KeepAlive"], "options": runtime_options(cfg)})
                if result.get("done") is not True or result.get("error"):
                    raise ValueError("完整窗口加载失败；原配置保留。")
                require_gpu_status(cfg, fetch("/api/ps"))
        legacy.unchanged(before)
        if updated != server:
            backup = server_path.with_name(server_path.name + ".before-multimodel-" +
                                          datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8] + ".bak")
            legacy.publish(backup, raw)
            legacy.unchanged(before)
            legacy.support.atomic_write(server_path, (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode())
    print("多模型注册完成；重启服务和本机OpenCode后用 /models 选择。", flush=True)
    if backup:
        print("服务配置备份：" + str(backup), flush=True)
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    args = parser.parse_args()
    try:
        enable(server_config=args.server_config)
        return 0
    except KeyboardInterrupt:
        print("已取消；原服务配置保留。")
    except Exception as error:
        print("多模型注册失败：" + (str(error) if isinstance(error, (ValueError, FileNotFoundError)) else type(error).__name__))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

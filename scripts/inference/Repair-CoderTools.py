"""停服修复已注册7B／14B工具模板；复用权重，备份清单与身份锁，失败回滚。"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server/src"), str(Path(__file__).parent)]
spec = importlib.util.spec_from_file_location("coder_tools_install", Path(__file__).with_name("Install-Qwen25Coder7B.py"))
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)
from coder_support import compatible_template, legacy_json_template, TOOL_TEMPLATE_VERSION, download, REGISTRY
from aiserver.inference.configuration import runtime_options
from aiserver.inference.gpu import require_gpu_status


def verify_call(fetch, cfg):
    """Real engine call and result roundtrip; no file or command is executed."""
    text_param = {"type": "object", "properties": {"filePath": {"type": "string"}}, "required": ["filePath"]}
    tools = [{"type": "function", "function": {"name": "read", "description": "Read a text file.", "parameters": text_param}},
             {"type": "function", "function": {"name": "glob", "description": "Find files.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
             {"type": "function", "function": {"name": "grep", "description": "Search text.", "parameters": text_param}},
             {"type": "function", "function": {"name": "write", "description": "Write a file.", "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}}, "required": ["filePath", "content"]}}},
             {"type": "function", "function": {"name": "edit", "description": "Edit a file.", "parameters": text_param}}]
    messages = [{"role": "system", "content": "You are opencode, an interactive CLI tool. Use read, glob, grep, write, or edit tools for file operations. You MUST call a tool instead of printing JSON that imitates one. Use the available file tool to obtain the note. Never guess its content."},
                {"role": "user", "content": "Find or read tool-proof.txt and quote the exact note."}]
    body = {"model": cfg["Model"], "messages": messages, "tools": tools, "stream": False,
            "truncate": False, "shift": False, "keep_alive": cfg["KeepAlive"],
            "options": {**runtime_options(cfg, 256), "temperature": 0, "seed": 42}}
    response = fetch("/api/chat", body)
    message = response.get("message", {})
    calls = message.get("tool_calls", [])
    if (response.get("done") is not True or response.get("done_reason") != "stop" or len(calls) != 1
            or calls[0].get("function", {}).get("name") not in {"read", "glob", "grep", "write", "edit"}
            or not isinstance(calls[0]["function"].get("arguments"), dict)):
        raise ValueError("工具模板已渲染，但真实引擎未返回原生文件工具调用；已停止，不宣告修复成功。")
    proof = "CODER_TOOL_PROOF_" + uuid4().hex
    tool_name = calls[0]["function"]["name"]
    body["messages"] = messages + [message, {"role": "tool", "tool_name": tool_name, "content": proof}]
    result = fetch("/api/chat", body)
    if (result.get("done") is not True or result.get("done_reason") != "stop"
            or proof not in result.get("message", {}).get("content", "")):
        raise ValueError("工具结果续答检查失败；已停止，不宣告修复成功。")
    if cfg.get("RequireFullGpu"):
        require_gpu_status(cfg, fetch("/api/ps"))


def repair(root=ROOT, server_config=None, *, variants=("7b", "14b"), engine_factory=install.legacy.temporary_engine):
    root = Path(root).resolve()
    server_path = install.legacy.select_config(root, server_config)
    server, server_raw = install.support.read_config(server_path)
    if server.get("provider") != "Ollama" or not server.get("manage_engine", True):
        raise ValueError("需要实际Ollama自管服务配置；请先停止AiServer。")
    selected = []
    before = {server_path: server_raw}
    names = {install.VARIANTS[v]["model"]: v for v in variants}
    for entry in [server["inference_config"], *server.get("additional_inference_configs", [])]:
        path = install.support.resolve(root, entry)
        cfg, raw = install.support.read_config(path)
        if cfg.get("Model") not in names:
            continue
        variant = names[cfg["Model"]]
        if any(item[0] == variant for item in selected):
            raise ValueError("Coder重复注册；请先核对服务配置。")
        if cfg.get("EngineVersion") != "0.40.9":
            raise ValueError("仅修复锁定的Ollama 0.40.9模板。")
        lock_path = install.support.resolve(root, cfg["ModelLockPath"])
        lock, lock_raw = install.support.read_config(lock_path)
        source_path = root / f"config/models/qwen25coder{variant}.source.json"
        source, source_raw = install.support.read_config(source_path)
        cache = root / install.VARIANTS[variant]["directory"] / "source/ollama.template"
        # The Ollama manifest digest is the model identity digest, not the
        # SHA256 of the local manifest file. Keep the identity lock as the
        # rollback boundary and verify the live model through /api/show/tags.
        before.update({path: raw, lock_path: lock_raw, source_path: source_raw})
        selected.append((variant, cfg, lock_path, lock, cache, source))
    if not selected:
        print("未注册所选7B／14B模型，无需修复。")
        return []
    operation = root / ".local/coder-tools-repair" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    changed, touched = [], []
    with install.legacy.offline(root, server):
        install.legacy.unchanged(before)
        try:
            for variant, cfg, lock_path, lock, cache, source in selected:
                # At most the pinned 1615-byte template is downloaded, never weights/tokenizers.
                download(REGISTRY + "/blobs/sha256:" + source["template_sha256"], cache,
                         source["template_sha256"], 1615)
                original = cache.read_text(encoding="utf-8")
                template = compatible_template(original)
                digest = hashlib.sha256(template.encode()).hexdigest()
                old_digest = hashlib.sha256(legacy_json_template(original).encode()).hexdigest()
                if lock.get("template_sha256") not in (old_digest, digest):
                    raise ValueError("存在其他自定义模板，拒绝覆盖。")
                if lock.get("template_sha256") == digest and lock.get("tool_template_version") == TOOL_TEMPLATE_VERSION:
                    print(variant + "工具模板已是当前版本。")
                    continue
                install.legacy.unchanged(before)
                backup = operation / variant
                install.publish(backup / "identity.before.json", before[lock_path])
                print(variant + "：升级XML工具模板并检查真实工具续答；备份：" + str(backup), flush=True)
                with engine_factory(root, cfg, backup) as fetch:
                    if fetch("/api/version").get("version") != cfg["EngineVersion"]:
                        raise ValueError("实际引擎版本不同。")
                    show = fetch("/api/show", {"model": cfg["Model"]})
                    current = next((m for m in fetch("/api/tags").get("models", []) if m.get("name") == cfg["Model"]), None)
                    if not current or current.get("digest") != lock.get("model", {}).get("digest"):
                        raise ValueError("实际模型摘要与身份锁不同。")
                    if hashlib.sha256(show.get("template", "").encode()).hexdigest() != lock["template_sha256"]:
                        raise ValueError("实际模板与身份锁不同。")
                    touched.append(lock_path)
                    created = fetch("/api/create", {"model": cfg["Model"], "from": cfg["Model"],
                                                    "template": template, "stream": False})
                    if created.get("status") != "success":
                        raise ValueError("Ollama模板更新未成功。")
                    show = fetch("/api/show", {"model": cfg["Model"]})
                    if (hashlib.sha256(show.get("template", "").encode()).hexdigest() != digest
                            or show.get("details", {}).get("quantization_level") != cfg["ExpectedQuantization"]
                            or not set(cfg["RequiredCapabilities"]).issubset(show.get("capabilities", []))
                            or any(show.get("model_info", {}).get(k) != v for k, v in lock.get("yarn", {}).items())):
                        raise ValueError("更新后模型身份、模板或YaRN元数据不符。")
                    if cfg.get("RequireFullGpu"):
                        require_gpu_status(cfg, fetch("/api/ps"))
                    # The model may choose to inspect a workspace with glob
                    # before read, so generation quality is not a safe
                    # migration gate. The render/parser path is verified by
                    # the isolated synthetic probe and the post-create
                    # identity checks above; real OpenCode behavior is tested
                    # separately by Check-ModelTools.py.
                    model = next(m for m in fetch("/api/tags")["models"] if m["name"] == cfg["Model"])
                    identity = {**lock, "model": model, "template_sha256": digest,
                                "tool_template_version": TOOL_TEMPLATE_VERSION, "capabilities": show["capabilities"]}
                install.legacy.unchanged(before)
                install.support.atomic_write(lock_path, (json.dumps(identity, ensure_ascii=False, indent=2)+"\n").encode())
                before[lock_path] = lock_path.read_bytes()
                changed.append({"model": cfg["Model"], "backup": str(backup), "tool_template_version": TOOL_TEMPLATE_VERSION})
        except BaseException:
            # Never rewrite weights; restore only the identity lock.
            for lock_path in reversed(touched):
                backup = next(operation / v for v, _, p, _, _, _ in selected if p == lock_path)
                install.support.atomic_write(lock_path, (backup / "identity.before.json").read_bytes())
            raise
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path)
    parser.add_argument("--models", nargs="+", choices=("7b", "14b"), default=["7b", "14b"])
    args = parser.parse_args()
    try:
        print(json.dumps(repair(server_config=args.server_config, variants=args.models), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print("修复未完成：" + (str(error) if type(error) is ValueError else type(error).__name__))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

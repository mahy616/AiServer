"""List only small server runtime files; no ZIP, model/data copy, or deployment."""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def inventory(root=ROOT, *, include_reference=True):
    root = Path(root).resolve()
    required = set()
    for folder, suffixes in (("server/src/aiserver", {".py", ".sql"}),
                             ("contracts/api/v1", {".json", ".md"})):
        required.update(path for path in (root / folder).rglob("*")
                        if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts)
    required.update(root / path for path in ("server/requirements.lock.txt", "server/pyproject.toml",
        "scripts/server/Run.py", "scripts/server/Manage.py", "ServerManager.cmd",
        "scripts/server/ServerManager.py", "scripts/server/ManagerActions.py", "scripts/server/manager_support.py"))
    version = tomllib.loads((root / "server/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if tuple(int(x) for x in version.split(".")[:2]) >= (0, 7):
        required.update(root / path for path in (
            "Install-Qwen25Coder7B.cmd", "scripts/inference/Install-Qwen25Coder7B.py",
            "scripts/inference/coder_support.py", "scripts/inference/Install-Qwen3VL8B.py",
            "scripts/inference/Prepare-AdditionalModel.py", "scripts/inference/Start-Ollama.py",
            "scripts/validation/Smoke-Coder.py",
            "config/inference.coder.example.json", "config/models/qwen25coder7b.source.json",
            "contracts/examples/opencode.example.json", "docs/正式工程运维.md"))
    if tuple(int(x) for x in version.split(".")[:2]) >= (0, 8):
        required.update(root / path for path in (
            "README.md",
            "Install-Qwen25Coder14B.cmd", "Test-Qwen25Coder14B.cmd",
            "scripts/inference/Install-Qwen25Coder14B.py", "scripts/validation/Check-CoderCapacity.py",
            "config/inference.coder14b.example.json", "config/models/qwen25coder14b.source.json",
            "contracts/examples/opencode.coder14b.example.json"))
    if tuple(int(x) for x in version.split(".")[:2]) >= (0, 9):
        required.update(root / path for path in ("scripts/validation/Check-OpenCodeCompaction.py",
            "contracts/examples/openai-context-error.json", "contracts/examples/openai-context-stream-error.sse"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 2):
        required.update(root / path for path in (
            "Enable-CoderModels.cmd", "Install-Qwen35.cmd", "Test-Qwen35.cmd", "Compare-Models.cmd",
            "scripts/inference/Enable-CoderModels.py", "scripts/inference/Install-Qwen35.py",
            "scripts/inference/model_registration.py", "scripts/validation/Compare-Models.py",
            "config/inference.qwen35.example.json", "config/models/qwen35.source.json",
            "Install-Qwen38.cmd", "scripts/inference/Install-Qwen38.py",
            "config/inference.qwen38.example.json",
            "scripts/client/OpenCode.py", "scripts/client/opencode_support.py", "scripts/client/Verify-OpenCode.py",
            "OpenCode-AiServer.cmd", "contracts/examples/opencode.multimodel.example.json"))
        required.update(root / path for path in (
            "scripts/validation/Benchmark-CSharp.py", "scripts/validation/Collect-CSharpBenchmark.py",
            "scripts/validation/csharp_benchmark/__init__.py", "scripts/validation/csharp_benchmark/common.py",
            "scripts/validation/csharp_benchmark/cases.py", "scripts/validation/csharp_benchmark/harness.py",
            "scripts/validation/csharp_benchmark/relay.py", "scripts/validation/csharp_benchmark/probes.py",
            "scripts/validation/csharp_benchmark/report.py"))
        required.add(root / "scripts/validation/csharp_benchmark/workflow.py")
        required.add(root / "scripts/inference/Repair-CoderTools.py")
        required.add(root / "scripts/validation/Check-ModelTools.py")
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 3):
        required.update(root / path for path in (
            "scripts/inference/pinned_model_install.py", "config/models/qwen38.source.json"))
        required.update(root / path for path in (
            "scripts/inference/qwen35_budget.py", "scripts/inference/Repair-InferenceRuntime.py"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 5):
        required.update(root / path for path in (
            "contracts/examples/openai-reasoning-request.json", "contracts/examples/openai-reasoning-response.json",
            "contracts/examples/openai-reasoning-stream.sse", "contracts/examples/reasoning-stream.sse"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 6):
        required.update(root / path for path in (
            "Remove-DeepSeek.cmd", "scripts/inference/Remove-DeepSeek.py",
            "scripts/deployment/deepseek-retired-files.json"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 7):
        required.update(root / path for path in (
            "contracts/examples/openai-truncated-error.json", "contracts/examples/openai-truncated-stream-error.sse"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 9):
        required.update(root / path for path in (
            "Test-ModelContexts128K.cmd",
            "Tune-ModelContexts.cmd", "scripts/validation/Tune-ModelContexts.py",
            "scripts/validation/context_capacity.py", "scripts/validation/gpu_capacity_sampler.py"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 10):
        required.update(root / name for name in ("Apply-5080ModelContexts.cmd", "Set-Qwen38-80K.cmd"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 11):
        retired = json.loads((root / "scripts/deployment/coder-retired-files.json").read_bytes())
        required.difference_update(root / path for path in retired)
        required.update(root / path for path in (
            "Use-9B27B.cmd", "scripts/inference/Use-9B27B.py", "scripts/deployment/coder-retired-files.json",
            "config/server.ollama.example.json",
            "contracts/examples/openai-tool-request.json", "contracts/examples/openai-tool-result-request.json",
            "contracts/examples/openai-tool-response.json", "contracts/examples/openai-stream.sse"))
    if tuple(int(x) for x in version.split(".")) >= (0, 9, 12):
        required.update(root / path for path in (
            "Install-ExtraModels.cmd", "Install-GptOss20B.cmd", "Install-Gemma4-12B.cmd", "Install-Gemma4-26B.cmd",
            "scripts/inference/Install-ExtraModels.py", "scripts/inference/Install-ExtraModels.ps1",
            "contracts/examples/openai-gptoss-request.json"))
        required.update(root / f"config/{folder}{key}.{suffix}.json"
                        for key in ("gptoss", "gemma12", "gemma26")
                        for folder, suffix in (("inference.", "example"), ("models/", "source")))
    if tuple(int(x) for x in version.split(".")) >= (0, 10, 0):
        required.update(root / path for path in (
            "contracts/examples/opencode.strata.example.json", "contracts/examples/openai-strata-request.json",
            "Install-Strata.cmd", "scripts/inference/Install-Strata.ps1", "scripts/inference/Install-Strata.py",
            "scripts/inference/strata_prepare.py", "scripts/inference/strata.requirements.txt",
            "scripts/validation/Test-Strata.py", "config/server.strata.example.json",
            "config/inference.strata.example.json", "config/models/strata.source.json"))
    if tuple(int(x) for x in version.split(".")) >= (0, 11, 0):
        required.update(root / path for path in (
            "Install-Strata-DeepSeek.cmd", "Test-StrataModels.cmd", "scripts/validation/Test-StrataModels.ps1",
            "config/server.strata.deepseek.example.json", "config/inference.strata.deepseek.example.json",
            "config/models/strata.deepseek.source.json", "contracts/examples/opencode.strata.deepseek.example.json",
            "contracts/examples/openai-strata-deepseek-request.json"))
    optional = [root / path for path in (
        "config/server.example.json", "config/server.ollama.example.json", "config/natapp.example.ini",
        "config/inference.5080.example.json", "config/inference.5080.8b.example.json",
        "scripts/inference/Prepare-AdditionalModel.py", "scripts/inference/Start-Ollama.py",
        "docs/正式工程运维.md")] if include_reference else []
    optional = [path for path in optional if path not in required]
    rows = []
    for group, paths in (("required", sorted(required)), ("reference", sorted(optional))):
        for path in paths:
            resolved = path.resolve()
            if (not resolved.is_relative_to(root) or not path.is_file() or
                    any(part in (".local",".git","__pycache__","bin","obj") for part in resolved.relative_to(root).parts)):
                raise ValueError("Missing or unexpected update file")
            data = path.read_bytes()
            rows.append({"group": group, "path":path.relative_to(root).as_posix(),
                         "bytes":len(data), "sha256":hashlib.sha256(data).hexdigest()})
    return rows


def main():
    parser = argparse.ArgumentParser()
    version = tomllib.loads((ROOT / "server/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    parser.add_argument("--output", type=Path, default=ROOT / ".local/updates" / ("server-"+version))
    args = parser.parse_args()
    rows = inventory()
    required = [row for row in rows if row["group"] == "required"]
    args.output.mkdir(parents=True, exist_ok=True)
    payload = {"version":version, "requiredFiles":len(required), "requiredBytes":sum(row["bytes"] for row in required),
               "referenceBytes":sum(row["bytes"] for row in rows if row["group"] == "reference"), "files":rows}
    (args.output / "files.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    lines = [f"# 5080服务器{version}手工复制清单", "", "本目录只有清单，没有打包或复制任何模型、运行库、凭据或业务数据。",
             "", f"必需：{len(required)}个文件，{payload['requiredBytes']}字节（{payload['requiredBytes']/1024:.1f}KiB）。",
             "", "保持相对于工程根的原目录覆盖；server/src/aiserver只复制.py/.sql，不复制__pycache__。",
             "私有server.local.json、NATAPP INI、.local、模型和引擎不在复制清单内。",
             "操作顺序和私有配置调整见工程docs/正式工程运维.md。", "", "| 类别 | 相对路径 | 字节 |", "|---|---|---:|"]
    lines += [f"| {'必需' if row['group']=='required' else '参考，可不复制'} | {row['path']} | {row['bytes']} |" for row in rows]
    (args.output / "files.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(f"Required: {len(required)} files, {payload['requiredBytes']} bytes ({payload['requiredBytes']/1024:.1f} KiB)")
    print(args.output / "files.md")


if __name__ == "__main__":
    main()

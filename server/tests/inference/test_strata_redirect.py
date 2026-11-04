"""Exercise the Windows migration launcher without models, network or GPU."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from aiserver.settings import ROOT

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")


def fixture(tmp_path, layout="four-shard"):
    root = tmp_path / "server with spaces"
    script = root / "scripts/inference/Redirect-StrataModels.ps1"
    script.parent.mkdir(parents=True)
    script.write_bytes((ROOT / "scripts/inference/Redirect-StrataModels.ps1").read_bytes())
    target = tmp_path / "new SSD" / "AiServer-Strata"
    files = []
    for suffix, folder in (("", "IQ3_XXS"), (".deepseek", "deepseek41/IQ2_XXS")):
        source = json.loads((ROOT / f"config/models/strata{suffix}.source.json").read_text(encoding="utf-8"))
        manifest = root / f"config/models/strata{suffix}.source.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        model_files = source["model_file_sets"][layout] if suffix and layout == "six-shard" else source["model_files"]
        for row in model_files:
            row["size"] = 4
            path = target / "models" / folder / row["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"tiny")
            files.append(path)
        manifest.write_text(json.dumps(source), encoding="utf-8")
    prefs = root / ".local/server-manager/launcher.json"
    prefs.parent.mkdir(parents=True)
    base = root / "config/server.local.json"
    base.write_text("{}", encoding="utf-8")
    prefs.write_text(json.dumps({"python": sys.executable, "config": str(base)}), encoding="utf-8")
    # Stand-in for the installer captures actual argv and allows either family to fail.
    (script.parent / "Install-Strata.py").write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(__file__).resolve().parents[2]\n"
        "with (root / 'calls.jsonl').open('a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "family = sys.argv[sys.argv.index('--family') + 1]\n"
        "sys.exit(7 if (root / ('fail-' + family)).exists() else 0)\n",
        encoding="utf-8",
    )
    return root, target, script, files


def launch(script, target, *extra):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return subprocess.run([str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                           "-StorageRoot", str(target), "-NoPause", *extra],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("failure", ["missing", "size"])
@pytest.mark.parametrize("layout", ["four-shard", "six-shard"])
def test_incomplete_deepseek_refuses_before_preparing_either_model(tmp_path, failure, layout):
    root, target, script, files = fixture(tmp_path, layout)
    if failure == "missing":
        files[-1].unlink()
    else:
        files[-1].write_bytes(b"incomplete")
    result = launch(script, target)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Nothing was redirected" in result.stdout
    assert not (root / "calls.jsonl").exists()


def test_check_only_and_relative_target_do_not_invoke_installer(tmp_path):
    root, target, script, _ = fixture(tmp_path)
    result = launch(script, target, "-CheckOnly")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SHA256 and model loading have not been checked" in result.stdout
    assert not (root / "calls.jsonl").exists()
    result = launch(script, "relative/path")
    assert result.returncode == 1
    assert "absolute local drive" in result.stdout


@pytest.mark.parametrize("failed_family", [None, "qwen", "deepseek"])
@pytest.mark.parametrize("layout", ["four-shard", "six-shard"])
def test_sequential_publication_and_failure_exit(tmp_path, failed_family, layout):
    root, target, script, _ = fixture(tmp_path, layout)
    if failed_family:
        (root / ("fail-" + failed_family)).touch()
    result = launch(script, target)
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [args[args.index("--family") + 1] for args in calls] == (
        ["qwen"] if failed_family == "qwen" else ["qwen", "deepseek"])
    for args in calls:
        assert args[args.index("--storage-root") + 1] == str(target)
        assert args[args.index("--server-config") + 1] == str(root / "config/server.local.json")
        assert "--offline" in args and "--install-dependencies" not in args
        assert "--context" not in args and "--max-output" not in args
        if args[args.index("--family") + 1] == "deepseek":
            assert args[args.index("--deepseek-layout") + 1] == layout
    assert result.returncode == (1 if failed_family else 0), result.stdout + result.stderr
    if failed_family == "deepseek":
        assert "Already redirected successfully: qwen" in result.stdout


def test_partial_six_shard_layout_reports_its_metadata_instead_of_old_files(tmp_path):
    root, target, script, files = fixture(tmp_path, "six-shard")
    files[2].unlink()  # The first DeepSeek shard is metadata, not a large weight.
    result = launch(script, target)
    assert result.returncode == 1
    assert "00001-of-00006.gguf" in result.stdout
    assert "00001-of-00004.gguf" not in result.stdout
    assert "UD-Q8_K_XL" not in result.stdout
    assert not (root / "calls.jsonl").exists()


def test_deepseek_only_does_not_require_or_repeat_qwen(tmp_path):
    root, target, script, files = fixture(tmp_path, "six-shard")
    for path in files[:2]:
        path.unlink()
    result = launch(script, target, "-Family", "deepseek")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1 and calls[0][calls[0].index("--family") + 1] == "deepseek"
    assert "--offline" in calls[0]

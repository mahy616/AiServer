"""Dependency failure/repair checks; no model download or real pip install."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/inference"))
spec = importlib.util.spec_from_file_location("coder_dependency_installer", ROOT / "scripts/inference/Install-Qwen25Coder7B.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

MISSING = [{"module": "gguf", "missing": "gguf", "kind": "ModuleNotFoundError"}]


def setup_repair(tmp_path, monkeypatch, replies):
    config = tmp_path / "server.local.json"
    config.write_text("{}")
    monkeypatch.setattr(installer.legacy, "select_config", lambda *args: config)
    lock = Mock(return_value=nullcontext())
    monkeypatch.setattr(installer.legacy, "offline", lock)
    probe = Mock(side_effect=replies)
    monkeypatch.setattr(installer, "probe_dependencies", probe)
    return probe, lock


def test_missing_dependency_names_interpreter_and_repair_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer, "probe_dependencies", lambda: MISSING)
    with pytest.raises(ValueError) as raised:
        installer.ensure_dependencies(tmp_path)
    assert "gguf" in str(raised.value) and sys.executable in str(raised.value)
    assert str(tmp_path / "server/requirements.lock.txt") in str(raised.value)
    assert sys.executable in capsys.readouterr().out


def test_repair_uses_selected_python_under_offline_lock_and_rechecks(tmp_path, monkeypatch):
    probe, lock = setup_repair(tmp_path, monkeypatch, [MISSING, []])
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(installer.subprocess, "run", run)
    installer.ensure_dependencies(tmp_path, repair=True)
    assert run.call_args.args[0] == [sys.executable, "-m", "pip", "install", "-r",
                                    str(tmp_path / "server/requirements.lock.txt")]
    assert probe.call_count == 2 and lock.call_count == 1


@pytest.mark.parametrize("returncode,after", [(1, []), (0, MISSING)])
def test_failed_repair_never_reports_ready(tmp_path, monkeypatch, returncode, after):
    setup_repair(tmp_path, monkeypatch, [MISSING, after])
    monkeypatch.setattr(installer.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], returncode)))
    with pytest.raises(ValueError, match="安装失败|仍无法导入"):
        installer.ensure_dependencies(tmp_path, repair=True)


def test_probe_failure_prevents_any_model_preparation(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["Install-Qwen25Coder7B.py"])
    monkeypatch.setattr(installer, "probe_dependencies", lambda: MISSING)
    prepare = Mock()
    monkeypatch.setattr(installer, "install", prepare)
    assert installer.main() == 1
    prepare.assert_not_called()


def test_real_probe_without_site_packages_lists_missing_modules(tmp_path):
    # Use the project's same absolute interpreter and disable site-packages for
    # this probe only. No package is uninstalled or installed on the real host.
    result = subprocess.run([sys.executable, "-S", "-X", "utf8", "-c", installer.DEPENDENCY_PROBE],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0
    issues = json.loads(result.stdout)
    assert any(item["module"] == "gguf" and item["missing"] == "gguf" for item in issues)
    assert any(item["module"] == "numpy" and item["missing"] == "numpy" for item in issues)
    assert not (tmp_path / ".local").exists()

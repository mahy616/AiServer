"""Real OpenCode and AiServer sockets; synthetic inference and isolated credentials."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from aiserver.app import create_app
from aiserver.inference.contracts import Profile
from aiserver.inference.fake import FakeBudget, FakeProvider
from aiserver.settings import ROOT, Settings

sys.path.insert(0, str(ROOT / "scripts/client"))
import opencode_support as support


def test_real_opencode_generated_text_agent_omits_tools(tmp_path, monkeypatch):
    if not support.BINARY.is_file():
        pytest.skip("Requires installed OpenCode")
    model = "fixture-text-model"
    cfg = json.loads((ROOT / "config/inference.coder.example.json").read_bytes())
    cfg.update(Model=model, RequiredCapabilities=["completion"])
    profile = Profile("synthetic-text", "Fake", json.dumps({"config": cfg}), ("text",), True)
    provider = FakeProvider(0)
    provider.supports_tools = False
    app = create_app(Settings(data_directory=tmp_path / "server", requests_per_minute=10000,
                              submissions_per_minute=1000),
                     profile=profile, budget=FakeBudget(), provider=provider)
    store = app.state.store
    store.initialize()
    store.user_add("fixture", "Synthetic fixture")
    key = store.token_create("fixture")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    until = time.monotonic() + 10
    while not server.started and time.monotonic() < until:
        time.sleep(.01)
    assert server.started
    monkeypatch.setattr(support, "WORK", tmp_path / "client")
    monkeypatch.setattr(support, "CONFIG", tmp_path / "client/opencode.json")
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + key}, trust_env=False) as client:
            result = {endpoint: client.get(endpoint).json() for endpoint in
                      ("/api/v1/models", "/api/v1/auth/me", "/api/v1/ready")}
        support.configure({"server": url}, result)
        config = json.loads(support.CONFIG.read_bytes())
        config.update(snapshot=False, share="disabled")
        config["agent"].update(title={"disable": True}, summary={"disable": True})
        support.CONFIG.write_bytes(support.json_bytes(config))
        assert config["default_agent"] == support.text_agent_name(model)
        assert config["provider"]["aiserver"]["models"][model]["limit"]["input"] == 73216
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENCODE_", "AISERVER_"))}
        env.update(OPENCODE_CONFIG=str(support.CONFIG), AISERVER_API_KEY=key,
                   OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_MODELS_FETCH="true",
                   OPENCODE_GIT_BASH_PATH="C:/Program Files/Git/bin/bash.exe",
                   NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
        for name in ("DATA", "CONFIG", "CACHE", "STATE"):
            directory = tmp_path / name.lower()
            directory.mkdir()
            env[f"XDG_{name}_HOME"] = str(directory)
        outcomes = []
        for agent in ("build", support.text_agent_name(model), None):
            command = [str(support.BINARY), "run", "--pure", "--format", "json"]
            if agent:
                command += ["--agent", agent]
            command += ["Reply with TEXT_MODE_OK using the supplied text."]
            process = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=90)
            output = process.stdout.replace(key, "[redacted]")
            label = agent or "default"
            (tmp_path / (label + ".jsonl")).write_text(output, encoding="utf-8")
            events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            errors = [e for e in events if e.get("type") == "error"]
            text = "".join(e.get("part", {}).get("text", "") for e in events if e.get("type") == "text")
            outcomes.append({"agent": label, "exit_code": process.returncode, "errors": len(errors),
                             "answered": "TEXT_MODE_OK" in text})
            if agent == "build":
                assert errors and "TOOLS_NOT_SUPPORTED" in output
                assert provider.calls == 0
            else:
                assert process.returncode == 0 and not errors and "TEXT_MODE_OK" in text, output
                assert not any(e.get("type") == "tool_use" for e in events)
        assert provider.calls == 2  # Both reached the tool-free provider; Build was rejected at admission.
        (tmp_path / "summary.json").write_text(json.dumps(outcomes, indent=2), encoding="utf-8")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()

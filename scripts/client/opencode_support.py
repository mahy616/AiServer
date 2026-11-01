"""Local OpenCode configuration and a separate Windows current-user key vault."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".local/opencode"
VAULT = WORK / "api-key.dpapi"
CONFIG = WORK / "opencode.json"
BINARY = ROOT / ".local/apps/opencode/node_modules/opencode-ai/bin/opencode.exe"
DEFAULT_SERVER = "https://szzj.nat200.top"


class SetupError(RuntimeError):
    pass


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def dpapi(raw, *, protect):
    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    incoming = Blob(len(raw), buffer)
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if protect:
        function = crypt.CryptProtectData
        description = "AiServer OpenCode independent API key"
    else:
        function = crypt.CryptUnprotectData
        description = None
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p if protect else ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                        wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    try:
        if not function(ctypes.byref(incoming), description, None, None, None, 1, ctypes.byref(outgoing)):
            raise SetupError("无法使用Windows当前用户解锁／保存OpenCode独立密钥。")
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        ctypes.memset(buffer, 0, len(raw))
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.length)
            kernel.LocalFree(outgoing.data)


def server_url(value):
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path or (parsed.scheme != "https" and not
                (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")))):
        raise SetupError("请输入HTTPS服务根地址（不含/v1、用户名或查询参数）。")
    return value


def key_record(server, key):
    key = key.strip()
    if not re.fullmatch(r"ais_[a-f0-9]{32}_[A-Za-z0-9_-]+", key):
        raise SetupError("密钥格式无效：请粘贴AiServer的token-create命令签发的完整key。")
    return {"server": server_url(server), "apiKey": key, "credential": "independent-api-key"}


def saved_key():
    if not VAULT.is_file():
        raise SetupError("尚未配置OpenCode独立API key；运行OpenCode-AiServer.cmd --set-key。")
    try:
        record = json.loads(dpapi(VAULT.read_bytes(), protect=False))
        if record["credential"] != "independent-api-key":
            raise ValueError()
        return key_record(record["server"], record["apiKey"])
    except (ValueError, KeyError):
        raise SetupError("OpenCode独立密钥文件无效，请用--set-key重新设置。") from None


def probe(record):
    result = {}
    with requests.Session() as client:
        client.trust_env = False
        client.headers["Authorization"] = "Bearer " + record["apiKey"]
        for endpoint in ("/api/v1/auth/me", "/api/v1/models", "/api/v1/ready"):
            try:
                response = client.get(record["server"] + endpoint, timeout=30, allow_redirects=False)
            except requests.RequestException:
                raise SetupError("无法连接AiServer，请检查网络、域名及5080服务。") from None
            if response.status_code == 401:
                raise SetupError("API key无效、已到期或已撤销；请在5080重新签发后用--set-key设置。")
            if response.status_code != 200:
                raise SetupError(f"AiServer检查失败：{endpoint}，HTTP {response.status_code}。")
            try:
                result[endpoint] = response.json()
            except ValueError:
                raise SetupError("AiServer返回了无效JSON。") from None
    return result


def configure(record, result):
    models = result["/api/v1/models"]["models"]
    if not models:
        raise SetupError("服务尚未发布模型。")
    cfg = json.loads((ROOT / "contracts/examples/opencode.example.json").read_text(encoding="utf-8"))
    provider = cfg["provider"]["aiserver"]
    provider["options"].update(baseURL=record["server"] + "/v1", apiKey="{env:AISERVER_API_KEY}")
    previous = {}
    if CONFIG.is_file():
        try:
            previous = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    descriptions = {}
    for item in models:
        descriptions[item["id"]] = {"name": item["id"], "tool_call": item.get("toolCall", False),
            "reasoning": item.get("thinking", False),
            "options": thinking_options(item),
            "modalities": {"input": item.get("inputTypes", ["text"]), "output": item.get("outputTypes", ["text"])},
            "limit": model_limits(item)}
        if item.get("thinking", False):
            descriptions[item["id"]]["variants"] = thinking_variants(item)
    provider["models"] = descriptions
    model = next((m for m in models if "aiserver/" + m["id"] == previous.get("model")), models[0])
    cfg.update(model="aiserver/" + model["id"], enabled_providers=["aiserver"], autoupdate=False)
    cfg.setdefault("agent", {}).update(text_agents(models))
    cfg["default_agent"] = "build" if model.get("toolCall", False) else text_agent_name(model["id"])
    cfg["compaction"].update(auto=True, prune=True)
    cfg["permission"].update(task="deny", webfetch="deny", websearch="deny")
    summary = {"server": record["server"], "model": model["id"], "models": list(descriptions),
               "limits": descriptions[model["id"]]["limit"],
               "canAcceptJobs": result["/api/v1/ready"]["canAcceptJobs"],
               "credential": "independent-api-key", "userId": result["/api/v1/auth/me"]["user"]["userId"],
               "expiresAt": result["/api/v1/auth/me"]["expiresAt"],
               "checkedAt": datetime.now(timezone.utc).isoformat()}
    atomic_write(CONFIG, json_bytes(cfg))
    atomic_write(WORK / "connection-summary.json", json_bytes(summary))
    return summary


def thinking_options(item):
    return {"think": item.get("defaultThinkingLevel", "medium") if item.get("thinkingLevels") else False}


def thinking_variants(item):
    if item.get("thinkingLevels"):
        return {level: {"think": level} for level in item["thinkingLevels"]}
    return {"normal": {"think": False}, "thinking": {"think": True}}


def model_limits(item):
    """OpenCode needs room for the default output, not the one-token maximum."""
    limits = item["limits"]
    context, output = limits["contextTokens"], limits["maxOutputTokens"]
    default_input = limits.get("defaultMaxInputTokens")
    if default_input is None and type(context) is int and type(output) is int:
        reserved = limits.get("reservedTokens")
        default_input = context - output - (reserved if type(reserved) is int else 512)
    maximum = limits["maxInputTokens"]
    return {"context": context, "input": min(maximum, default_input)
            if type(maximum) is int and type(default_input) is int else maximum, "output": output}


def text_agent_name(model_id):
    return "text-" + hashlib.sha256(model_id.encode()).hexdigest()[:10]


def text_agents(models):
    return {text_agent_name(item["id"]): {
        "description": item["id"] + " — 文字问答，无文件或命令工具",
        "mode": "primary", "model": "aiserver/" + item["id"],
        "tools": {"*": False}, "permission": {"*": "deny"},
        "prompt": "Answer using only material provided in the conversation. You have no tools and cannot read or change local files or run commands. Ask the user to paste any needed material; return code as text."
    } for item in models if not item.get("toolCall", False)}


def install_key(server, key):
    record = key_record(server, key)
    result = probe(record)
    # Confirm both API compatibility and current-user DPAPI before replacing a working key.
    encrypted = dpapi(json_bytes(record), protect=True)
    if json.loads(dpapi(encrypted, protect=False)) != record:
        raise SetupError("密钥加密回读校验失败，未替换原密钥。")
    summary = configure(record, result)
    atomic_write(VAULT, encrypted)
    return summary


def refresh_connection():
    record = saved_key()
    return configure(record, probe(record))


def environment(read_only=False):
    record = saved_key()
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if cfg["provider"]["aiserver"]["options"]["baseURL"] != record["server"] + "/v1":
        raise SetupError("配置地址与独立密钥绑定地址不一致。")
    env = dict(os.environ)
    env.update(AISERVER_API_KEY=record["apiKey"], OPENCODE_CONFIG=str(CONFIG),
               OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_GIT_BASH_PATH="C:/Program Files/Git/bin/bash.exe")
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY", ""), urlsplit(record["server"]).hostname,
                                            "127.0.0.1", "localhost", "::1"]))
    env["no_proxy"] = env["NO_PROXY"]
    # Ignore inherited inline overrides unless explicitly supplied by this launcher.
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    for key, folder in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"),
                        ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        path = WORK / folder
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    if read_only:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permission": {"*": "deny", "read": "allow",
            "glob": "allow", "grep": "allow", "list": "allow", "todowrite": "allow"}})
    return env

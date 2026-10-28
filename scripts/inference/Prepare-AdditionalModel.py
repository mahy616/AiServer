"""Register an already installed local model without changing the active server config.

Uses only the Python standard library. Never downloads models, generates text,
starts/stops processes, or replaces existing files with different contents.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[2]
PROCESS_KEYS = ("BaseUrl", "ExecutablePath", "ModelsPath", "EngineVersion",
                "MaxLoadedModels", "NumParallel", "EngineMaxQueue")
TEMPLATE = "config/inference.5080.8b.example.json"
OUTPUT_CONFIG = "config/inference.5080.8b.local.json"
OUTPUT_PATCH = "config/server.dual-model.patch.local.json"
MAX_METADATA_BYTES = 4 * 1024 * 1024


def read_json(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalOllama:
    def __init__(self, base_url, *, generate_timeout=600):
        uri = urlsplit(base_url)
        if (uri.scheme != "http" or uri.hostname != "127.0.0.1"
                or uri.username or uri.password or uri.query or uri.fragment
                or uri.path not in ("", "/")):
            raise ValueError("BaseUrl must be loopback HTTP at 127.0.0.1")
        self.base_url = base_url.rstrip("/")
        if type(generate_timeout) not in (int, float) or not 0 < generate_timeout <= 86400:
            raise ValueError("Invalid local generation timeout")
        self.generate_timeout = generate_timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def __call__(self, endpoint, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.base_url + endpoint, data=data,
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        with self.opener.open(request, timeout=self.generate_timeout if endpoint in ("/api/create", "/api/generate", "/api/chat") else 15) as response:
            raw = response.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise ValueError("Ollama metadata exceeded the size limit")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid Ollama metadata")
        return value


def source_config(root, server_config):
    server_path = (root / server_config).resolve()
    server = read_json(server_path)
    if server.get("provider") != "Ollama":
        raise ValueError("The selected server config must use provider=Ollama")
    primary = read_json((root / server["inference_config"]).resolve())
    return server, primary


def comparable(value, identity=False):
    value = deepcopy(value)
    if identity:
        value.get("model", {}).pop("modified_at", None)
    return value


def plan(root, server_config, *, model=None, quantization=None, fetch=None):
    root = Path(root).resolve()
    server, primary = source_config(root, server_config)
    cfg = read_json(root / TEMPLATE)
    for key in PROCESS_KEYS:
        cfg[key] = primary[key]
    if model is not None:
        cfg["Model"] = model
    if quantization is not None:
        cfg["ExpectedQuantization"] = quantization
    if not isinstance(cfg["Model"], str) or not 1 <= len(cfg["Model"]) <= 128:
        raise ValueError("Invalid model name")
    if cfg["Model"] == primary["Model"]:
        raise ValueError("The additional model must differ from the primary model")
    if any(cfg[key] != 1 for key in ("MaxLoadedModels", "NumParallel", "EngineMaxQueue")):
        raise ValueError("This deployment requires one engine execution slot")
    # This metadata-only helper also ships independently of the server package.
    # MaxInputTokens is legacy metadata; validate the actual window/output pair.
    if (any(type(cfg[key]) is not int for key in ("NumCtx", "NumPredict", "ReservedTokens"))
            or cfg["NumPredict"] < 1 or cfg["ReservedTokens"] < 0
            or cfg["NumCtx"] <= cfg["NumPredict"] + cfg["ReservedTokens"]):
        raise ValueError("Invalid context/output partition in the 8B template")

    config_path = root / OUTPUT_CONFIG
    identity_path = (root / cfg["ModelLockPath"]).resolve()
    if not identity_path.is_relative_to(root / "config"):
        raise ValueError("The new model lock must stay inside the project config directory")
    if identity_path in (config_path, root / OUTPUT_PATCH):
        raise ValueError("The inference config, model lock and server patch must be separate files")
    additional = list(server.get("additional_inference_configs", []))
    for path in additional:
        resolved = (root / path).resolve()
        if resolved != config_path and read_json(resolved)["Model"] == cfg["Model"]:
            raise ValueError("This model is already registered by another inference config")
    if not any((root / path).resolve() == config_path for path in additional):
        additional.append(OUTPUT_CONFIG)
    if len(additional) > 7:
        raise ValueError("At most seven additional model configs are supported")

    fetch = fetch or LocalOllama(primary["BaseUrl"])
    version = fetch("/api/version")
    if version.get("version") != cfg["EngineVersion"]:
        raise ValueError("Ollama version differs from EngineVersion in the primary config")
    tags = fetch("/api/tags")
    entries = [item for item in tags.get("models", []) if item.get("name") == cfg["Model"]]
    if len(entries) != 1:
        raise ValueError("Model not found in this Ollama model store. Run --list and check ModelsPath.")
    entry = entries[0]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", entry.get("digest", "")):
        raise ValueError("Ollama did not return a full SHA256 model digest")
    show = fetch("/api/show", {"model": cfg["Model"]})
    expected = cfg["ExpectedQuantization"]
    if any(info.get("details", {}).get("quantization_level") != expected for info in (entry, show)):
        raise ValueError("Model quantization differs from ExpectedQuantization; inspect the installed model")
    capabilities = show.get("capabilities", [])
    if not set(cfg["RequiredCapabilities"]).issubset(capabilities):
        raise ValueError("The model must provide the required completion and vision capabilities")
    if not isinstance(show.get("template"), str) or not show["template"]:
        raise ValueError("Ollama did not return a nonempty model template")
    identity = {"engine_version": version["version"], "model": entry,
                "template_sha256": hashlib.sha256(show["template"].encode("utf-8")).hexdigest(),
                "capabilities": capabilities, "image_policy": cfg["ImagePolicy"]}
    patch = {"inference_config": server["inference_config"], "additional_inference_configs": additional}
    outputs = [(config_path, cfg, False), (identity_path, identity, True),
               (root / OUTPUT_PATCH, patch, False)]
    # Inspect EVERY destination before creating any file. Preserve prior target-machine work.
    for path, value, is_identity in outputs:
        if path.exists() and comparable(read_json(path), is_identity) != comparable(value, is_identity):
            raise ValueError(f"Existing file differs; inspect it before preparing again: {path}")
    result = {"model": cfg["Model"], "digest": entry["digest"],
              "config": str(config_path), "lock": str(identity_path), "patch": str(root / OUTPUT_PATCH)}
    return outputs, result


def prepare(root, server_config, *, model=None, quantization=None, fetch=None):
    outputs, result = plan(root, server_config, model=model, quantization=quantization, fetch=fetch)
    for path, value, _ in outputs:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path, default=Path("config/server.local.json"))
    parser.add_argument("--model", help="Exact installed 8B model name from --list")
    parser.add_argument("--quantization", help="Expected quantization, default Q4_K_M from the template")
    parser.add_argument("--list", action="store_true", help="Only list the models in the primary config's Ollama")
    args = parser.parse_args()
    try:
        if args.list:
            _, primary = source_config(ROOT, args.server_config)
            tags = LocalOllama(primary["BaseUrl"])("/api/tags")
            for item in tags.get("models", []):
                print(json.dumps({"name": item.get("name"), "digest": item.get("digest"),
                                  "quantization": item.get("details", {}).get("quantization_level")},
                                 ensure_ascii=False))
        else:
            result = prepare(ROOT, args.server_config, model=args.model, quantization=args.quantization)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("Prepared only. Stop the service, then merge the patch into the selected server JSON.")
        return 0
    except (OSError, URLError, ValueError, KeyError, TypeError) as error:
        # Do not print response bodies or private configuration contents.
        if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            message = str(error)
        else:
            message = "Check JSON files and the existing loopback Ollama process (" + type(error).__name__ + ")."
        print("Preparation failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

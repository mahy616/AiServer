"""Offline validation of shipped references and request/response/SSE examples."""
import json
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))
from jsonschema import Draft202012Validator, FormatChecker
from aiserver import __version__
from aiserver.api.validation import RequestValidator
from aiserver.api.openai import ChatValidator


def main():
    api, samples = ROOT / "contracts/api/v1", ROOT / "contracts/examples"
    contract = json.loads((api / "openapi.json").read_text(encoding="utf-8"))
    version = tomllib.loads((ROOT / "server/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == version, "Python package version differs from the server release"
    assert contract["info"]["version"] == version, "OpenAPI version differs from the server release"
    assert contract["security"] == [{"bearerAuth": []}]
    assert contract["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert not {"/api/v1/auth/login", "/api/v1/auth/logout"} & contract["paths"].keys()
    responses = json.loads((api / "responses.schema.json").read_text(encoding="utf-8"))
    for name in ("text-request", "image-request", "four-image-request"):
        RequestValidator(api).validate((samples / (name + ".json")).read_bytes())
    for name, definition in (("submission", "accepted"), ("status", "status"),
                             ("result-fake", "result"), ("ready-fake", "ready"), ("error", "errorResponse"),
                             ("me", "me")):
        schema = dict(responses, **{"$ref": "#/$defs/" + definition})
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(
            json.loads((samples / (name + ".json")).read_text(encoding="utf-8")))
    schema = json.loads((api / "sse-event.schema.json").read_text(encoding="utf-8").replace(
        "responses.schema.json#/$defs/", "#/$defs/"))
    schema["$defs"] = responses["$defs"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for filename in ("stream.sse", "reasoning-stream.sse"):
        for block in (samples / filename).read_text(encoding="utf-8").strip().split("\n\n"):
            lines = block.splitlines()
            validator.validate({"event": lines[0][7:], "data": json.loads(lines[1][6:])})

    # Public API examples must remain consistent with the shipped schemas.
    guide = (api / "README.md").read_text(encoding="utf-8")
    assert (api / "接口文档.md").read_text(encoding="utf-8") == guide, "Client delivery guide differs from the canonical API guide"
    examples = re.findall(r"<!-- example:([\w-]+) -->\s*~~~(json|text)\n(.*?)\n~~~", guide, re.S)
    assert len(examples) == 12, "Missing API-guide examples"
    for name, language, content in examples:
        if name == "sse":
            for block in content.strip().split("\n\n"):
                lines = block.splitlines()
                if lines[0].startswith(":"):
                    continue
                validator.validate({"event": lines[0][7:], "data": json.loads(lines[1][6:])})
        elif name == "request":
            RequestValidator(api).validate(content.encode("utf-8"))
        else:
            inline_schema = dict(responses, **{"$ref": "#/$defs/" + name})
            Draft202012Validator(inline_schema, format_checker=FormatChecker()).validate(json.loads(content))

    openai = json.loads((api / "openai-responses.schema.json").read_text(encoding="utf-8"))
    for name in ("openai-tool-request", "openai-tool-result-request", "openai-reasoning-request", "openai-gptoss-request", "openai-strata-request", "openai-strata-deepseek-request"):
        ChatValidator(api).validate((samples / (name + ".json")).read_bytes())
    Draft202012Validator(dict(openai, **{"$ref": "#/$defs/completion"})).validate(
        json.loads((samples / "openai-tool-response.json").read_text(encoding="utf-8")))
    Draft202012Validator(dict(openai, **{"$ref": "#/$defs/completion"})).validate(
        json.loads((samples / "openai-reasoning-response.json").read_text(encoding="utf-8")))
    for line in (samples / "openai-stream.sse").read_text(encoding="utf-8").splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            Draft202012Validator(dict(openai, **{"$ref": "#/$defs/chunk"})).validate(json.loads(line[6:]))
    for line in (samples / "openai-reasoning-stream.sse").read_text(encoding="utf-8").splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            Draft202012Validator(dict(openai, **{"$ref": "#/$defs/chunk"})).validate(json.loads(line[6:]))
    Draft202012Validator(dict(openai, **{"$ref": "#/$defs/error"})).validate(
        json.loads((samples / "openai-truncated-error.json").read_text(encoding="utf-8")))
    truncated = (samples / "openai-truncated-stream-error.sse").read_text(encoding="utf-8")
    assert "[DONE]" not in truncated
    for line in truncated.splitlines():
        if line.startswith("data: "):
            value = json.loads(line[6:])
            Draft202012Validator(dict(openai, **{"$ref": "#/$defs/error"})).validate(value)
            detail = json.loads(value["error"]["message"])["error"]
            assert detail["code"] == "invalid_prompt" and detail["aiserverCode"] == "OUTPUT_TRUNCATED"
    additional = re.findall(r"<!-- openai-example:([\w-]+) -->\s*~~~json\n(.*?)\n~~~", guide, re.S)
    assert len(additional) == 3, "Missing OpenAI/OpenCode standalone examples"
    for name, content in additional:
        source = {"config": "opencode.example.json", "request": "openai-tool-request.json",
                  "tool-result": "openai-tool-result-request.json"}[name]
        assert json.loads(content) == json.loads((samples / source).read_text(encoding="utf-8"))
        if name == "config":
            models = json.loads(content)["provider"]["aiserver"]["models"]
            for filename in ("inference.qwen35.example.json", "inference.qwen38.example.json"):
                cfg = json.loads((ROOT / "config" / filename).read_text(encoding="utf-8"))
                assert models[cfg["Model"]]["limit"] == {"context": cfg["NumCtx"], "input": cfg["NumCtx"]-cfg["ReservedTokens"]-cfg["NumPredict"], "output": cfg["NumPredict"]}, \
                    "OpenCode limits differ from " + filename
                assert cfg["NumPredict"] + cfg["ReservedTokens"] < cfg["NumCtx"], \
                    "Invalid context/output partition in " + filename
        else:
            ChatValidator(api).validate(content.encode("utf-8"))


    multi = json.loads((samples / "opencode.multimodel.example.json").read_text(encoding="utf-8"))
    strata = json.loads((ROOT / "config/inference.strata.example.json").read_bytes())
    strata_client = json.loads((samples / "opencode.strata.example.json").read_bytes())
    entry = strata_client["provider"]["aiserver"]["models"][strata["Model"]]
    assert entry["limit"] == {"context": strata["NumCtx"], "input": strata["NumCtx"]-strata["NumPredict"]-strata["ReservedTokens"], "output": strata["NumPredict"]}
    assert entry["variants"] == {"normal": {"think": False}, "thinking": {"think": True}}
    assert entry["tool_call"] and entry["reasoning"] and entry["modalities"]["input"] == ["text"]
    assert strata_client["provider"]["aiserver"]["options"]["apiKey"] == "{env:AISERVER_API_KEY}"
    deepseek = json.loads((ROOT / "config/inference.strata.deepseek.example.json").read_bytes())
    deepseek_client = json.loads((samples / "opencode.strata.deepseek.example.json").read_bytes())
    entry = deepseek_client["provider"]["aiserver"]["models"][deepseek["Model"]]
    assert entry["limit"] == {"context": deepseek["NumCtx"], "input": deepseek["NumCtx"]-deepseek["NumPredict"]-deepseek["ReservedTokens"], "output": deepseek["NumPredict"]}
    assert entry["variants"] == {"normal": {"think": False}, "thinking": {"think": True}}
    assert entry["tool_call"] and entry["reasoning"] and entry["modalities"]["input"] == ["text"]
    assert deepseek_client["provider"]["aiserver"]["options"]["apiKey"] == "{env:AISERVER_API_KEY}"
    for filename in ("opencode.example.json", "opencode.multimodel.example.json"):
        options = json.loads((samples / filename).read_text(encoding="utf-8"))["provider"]["aiserver"]["options"]
        assert options["apiKey"] == "{env:AISERVER_API_KEY}", "Public config must reference the personal API key"
        assert options["baseURL"].endswith("/v1")
    for filename in ("inference.qwen35.example.json", "inference.qwen38.example.json"):
        cfg = json.loads((ROOT / "config" / filename).read_text(encoding="utf-8"))
        assert multi["provider"]["aiserver"]["models"][cfg["Model"]]["limit"] == {
            "context": cfg["NumCtx"], "input": cfg["NumCtx"]-cfg["ReservedTokens"]-cfg["NumPredict"], "output": cfg["NumPredict"]}
        assert multi["provider"]["aiserver"]["models"][cfg["Model"]]["tool_call"] == ("tools" in cfg["RequiredCapabilities"])
        entry = multi["provider"]["aiserver"]["models"][cfg["Model"]]
        assert entry["options"] == {"think": False}
        assert bool(entry.get("variants")) == entry["reasoning"]

    error_validator = Draft202012Validator(dict(openai, **{"$ref": "#/$defs/error"}))
    for key in ("gptoss", "gemma12", "gemma26"):
        cfg = json.loads((ROOT / f"config/inference.{key}.example.json").read_bytes())
        entry = multi['provider']['aiserver']['models'][cfg['Model']]
        assert entry['limit'] == {'context': cfg['NumCtx'], 'input': cfg['NumCtx']-cfg['ReservedTokens']-cfg['NumPredict'], 'output': cfg['NumPredict']}
        levels = cfg.get('ThinkingLevels')
        assert entry['options'] == {'think': cfg['DefaultThinkingLevel'] if levels else False}
        assert entry['variants'] == ({x: {'think': x} for x in levels} if levels else {'normal': {'think': False}, 'thinking': {'think': True}})
        assert entry['tool_call'] and entry['reasoning'] and entry['modalities']['input'] == ['text']
    error_validator.validate(json.loads((samples / "openai-context-error.json").read_text(encoding="utf-8")))
    for line in (samples / "openai-context-stream-error.sse").read_text(encoding="utf-8").splitlines():
        if line.startswith("data: "):
            value = json.loads(line[6:])
            error_validator.validate(value)
            envelope = json.loads(value["error"]["message"])
            assert envelope["type"] == "error" and envelope["error"]["code"] == "context_length_exceeded"

    def references(value, source):
        if isinstance(value, dict):
            if "$ref" in value:
                filename, _, pointer = value["$ref"].partition("#")
                target = json.loads((source.parent / filename if filename else source).read_text(encoding="utf-8"))
                for segment in pointer.lstrip("/").split("/") if pointer else ():
                    target = target[segment.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                references(child, source)
        elif isinstance(value, list):
            for child in value:
                references(child, source)
    for path in api.glob("*.json"):
        references(json.loads(path.read_text(encoding="utf-8")), path)
    print("PASS: contract references, native/OpenAI samples, 12 native + 3 OpenAI/OpenCode inline examples")


if __name__ == "__main__":
    main()

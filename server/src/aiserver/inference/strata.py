"""One managed Strata model; OpenAI SSE is translated to the existing job contract."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx

from aiserver.context.prompt_builder import prepare_messages, PreparedPrompt
from aiserver.context.input_budget import check_budget
from aiserver.context.window_budget import freeze_output, validate_window, budget_details, OutputLimitExceeded
from aiserver.inference.configuration import generation_defaults
from aiserver.inference.contracts import BudgetCheck, PreparedInput, Profile, Generation, ModelError
from aiserver.inference.ollama import engine_error
from aiserver.operations.engine import ManagedEngine
from aiserver.settings import ROOT


def path_of(value):
    return (ROOT / value).resolve()


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("Nonfinite JSON number")
    def number(raw):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("Nonfinite JSON number")
        return value
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def is_deepseek(cfg):
    return cfg.get("ModelFamily", "qwen4exp") == "deepseek41"


def identity_model_path(identity):
    return identity.get("engine_model_path", identity.get("native_path"))


def validate_config(cfg):
    validate_window(cfg)
    generation_defaults(cfg)
    uri = urlsplit(cfg["BaseUrl"])
    if (uri.scheme != "http" or uri.hostname != "127.0.0.1" or not uri.port or
            uri.username or uri.password or uri.query or uri.fragment or uri.path not in ("", "/")):
        raise ValueError("Strata requires local loopback HTTP")
    family = cfg.get("ModelFamily", "qwen4exp")
    quant = "IQ2_XXS" if is_deepseek(cfg) else "IQ3_XXS"
    if (family not in ("qwen4exp", "deepseek41") or cfg.get("Provider") != "Strata" or cfg.get("ExpectedQuantization") != quant or
            cfg.get("EngineVersion") != "0.1.55" or cfg.get("RuntimeVersion") != "0.2.9" or
            cfg.get("NumParallel") != 1 or cfg.get("MaxLoadedModels") != 1 or cfg.get("EngineMaxQueue") != 1 or
            cfg.get("ImagePolicy", {}).get("MaxImages") != 0 or cfg.get("Budget", {}).get("Method") != "conservative-estimate-v1"):
        raise ValueError("Strata requires one text-only Qwen IQ3_XXS or DeepSeek IQ2_XXS model")
    if is_deepseek(cfg) and (type(cfg.get("ActiveExperts")) is not int or cfg["ActiveExperts"] not in (5, 6) or
                             cfg.get("DraftMode") != "auto"):
        raise ValueError("DeepSeek requires explicit active experts and DSpark auto")
    for key in ("GenerateTimeoutSeconds", "ConnectTimeoutSeconds", "StartupTimeoutSeconds", "MaxResponseBytes"):
        if type(cfg[key]) not in (int, float) or not 0 < cfg[key] <= 86400 * 1024:
            raise ValueError("Invalid Strata resource limit")
    if not 8192 <= cfg["NumCtx"] <= 262144 or not Path(cfg["ExecutablePath"]).is_absolute():
        raise ValueError("Invalid Strata context/Python path")
    if cfg.get("ThinkingLevels") or cfg.get("RequireFullGpu"):
        raise ValueError("Strata v1 uses boolean thinking and hybrid placement")


def verify_local_files(cfg, identity):
    """Full content hashes are checked by preparation; large files use recorded size/mtime at startup."""
    if (identity.get("provider") != "Strata" or identity.get("model") != cfg["Model"] or
            identity.get("engine_version") != cfg["EngineVersion"] or identity.get("release") != cfg["RuntimeVersion"]):
        raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
    files = identity.get("files", [])
    if not files:
        raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
    for row in files:
        file = path_of(row["path"])
        stat = file.stat()
        if stat.st_size != row["size"] or ("mtime_ns" in row and stat.st_mtime_ns != row["mtime_ns"]):
            raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
        if row.get("startup_hash", True) and digest(file) != row["sha256"]:
            raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
    run = strict_json(path_of(cfg["RunConfigPath"]).read_bytes())
    args = run.get("args", [])
    model_path = path_of(identity_model_path(identity))
    required_files = {path_of(cfg["EnginePath"]), path_of(cfg["RuntimePath"]) / "serve/server.py"}
    required_files.update({model_path / "ds4.txt", model_path / "tensors.txt", model_path / "engram.bin"}
                          if is_deepseek(cfg) else {model_path})
    if (not required_files.issubset({path_of(row["path"]) for row in files}) or
            path_of(run["exe"]) != path_of(cfg["EnginePath"]) or
            path_of(run["cwd"]) != path_of(cfg["RuntimePath"]) or
            path_of(args[args.index("--pack" if is_deepseek(cfg) else "--native") + 1]) != model_path):
        raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
    if (run.get("fit_max_tokens") is not False or run.get("parallel", 1) != 1 or run.get("max_queue") != 1 or
            run.get("model_name") != cfg["Model"] or
            run.get("host") != "127.0.0.1" or run.get("lazy_load", False) or
            str(cfg["NumCtx"]) != args[args.index("--max-context") + 1] or
            "--batch" in args or "--batch-groups" in args):
        raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
    if is_deepseek(cfg):
        if (identity.get("family") != "deepseek41" or run.get("arch") != "deepseek41" or
                run.get("env", {}).get("STRATA_DS4_SLOT_KEEP") != "0" or
                run.get("env", {}).get("STRATA_DS4_AUTO_PIN") != "0" or
                args[args.index("--experts") + 1] != str(cfg["ActiveExperts"]) or
                args[args.index("--vision") + 1] != "off" or args[args.index("--dspark") + 1] != cfg["DraftMode"] or
                "--native" in args or "--mtp" in args):
            raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
    elif run.get("mtp") is not True or "--mtp" not in args or run.get("arch", "qwen4exp") != "qwen4exp":
        raise ValueError("MODEL_IDENTITY_CHECK_FAILED")
    if identity.get("run_config_sha256") != digest(path_of(cfg["RunConfigPath"])):
        raise ValueError("MODEL_IDENTITY_CHECK_FAILED")


def load_strata_profile(settings):
    cfg = strict_json(settings.inference_config.read_bytes())
    validate_config(cfg)
    identity = strict_json(path_of(cfg["ModelLockPath"]).read_bytes())
    verify_local_files(cfg, identity)
    snapshot = json.dumps({"config": cfg, "identity": identity, "messageMappingVersion": "strata-chat-v1",
                           "capacityMethod": "conservative-estimate-v1"}, ensure_ascii=False, sort_keys=True)
    profile = Profile(cfg["ProfileId"] + "-" + hashlib.sha256(snapshot.encode()).hexdigest()[:12],
                      "Strata", snapshot, ("text",), False)
    return cfg, identity, profile


class StrataBudget:
    accepted_input_types = ("text",)

    def __init__(self, cfg):
        self.cfg = cfg

    async def prepare(self, task, profile):
        try:
            prepared = prepare_messages(strict_json(task.request_json), task.images, self.cfg["ImagePolicy"], provider="Strata")
            prepared = freeze_output(prepared, self.cfg, supports_thinking=True)
        except OutputLimitExceeded:
            return BudgetCheck(code="OUTPUT_LIMIT_EXCEEDED", details={"maxOutputTokens": self.cfg["NumPredict"], "field": "max_tokens"})
        except ValueError:
            return BudgetCheck(code="IMAGE_LIMIT_EXCEEDED" if task.images else "INVALID_REQUEST")
        decision = check_budget(prepared, self.cfg)
        if not decision.ready:
            return BudgetCheck(code=decision.code, details=budget_details(self.cfg, prepared, decision.estimated_input_tokens))
        return BudgetCheck(PreparedInput(profile.profile_id, json.dumps(asdict(prepared), ensure_ascii=False),
            prepared.material_sha256(), "conservative-estimate-v1", prepared.prompt_version,
            estimated_input_tokens=decision.estimated_input_tokens))


def chat_payload(cfg, prepared):
    options = strict_json(prepared.options_json)
    output = options.pop("num_predict")
    payload = {"model": cfg["Model"], "messages": prepared.messages(), "stream": True,
               "stream_options": {"include_usage": True}, "max_tokens": output,
               "reasoning_effort": "high" if prepared.think else "none", **options}
    tools = strict_json(prepared.tools_json)
    if tools:
        payload.update(tools=tools, tool_choice=prepared.tool_choice, parallel_tool_calls=prepared.parallel_tool_calls)
    return payload


class StrataAdapter:
    def __init__(self, cfg):
        validate_config(cfg)
        self.cfg = cfg
        self.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(cfg["GenerateTimeoutSeconds"], connect=cfg["ConnectTimeoutSeconds"]))

    async def aclose(self):
        await self.client.aclose()

    async def chat(self, prompt, on_delta):
        cfg = self.cfg
        parts, reasoning, calls = [], [], {}
        finished, done, usage, total = None, False, {}, 0
        allowed = {t["function"]["name"] for t in strict_json(prompt.tools_json)}
        started = time.monotonic()

        def protocol(complete=False):
            return ModelError("MODEL_PROTOCOL_ERROR", request_finished=complete)

        async def consume(data):
            nonlocal finished, done, usage
            if data == "[DONE]":
                done = True
                return
            value = strict_json(data)
            if not isinstance(value, dict) or value.get("error"):
                raise protocol()
            if value.get("model", cfg["Model"]) != cfg["Model"]:
                raise protocol()
            if value.get("usage") is not None:
                if not isinstance(value["usage"], dict):
                    raise protocol()
                usage = value["usage"]
            choices = value.get("choices", [])
            if not isinstance(choices, list) or len(choices) > 1:
                raise protocol()
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0 or finished is not None:
                    raise protocol()
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise protocol()
                for key, target, channel in (("content", parts, "content"), ("reasoning_content", reasoning, "reasoning_content")):
                    text = delta.get(key)
                    if text is not None and not isinstance(text, str):
                        raise protocol()
                    if text:
                        target.append(text)
                        await on_delta(text, channel)
                tool_fragments = delta.get("tool_calls", [])
                if not isinstance(tool_fragments, list):
                    raise protocol()
                for fragment in tool_fragments:
                    if not isinstance(fragment, dict):
                        raise protocol()
                    index = fragment.get("index")
                    if type(index) is not int or not 0 <= index < 128:
                        raise protocol()
                    call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if fragment.get("type", "function") != "function":
                        raise protocol()
                    if "id" in fragment:
                        if not isinstance(fragment["id"], str):
                            raise protocol()
                        call["id"] += fragment["id"]
                    function = fragment.get("function", {})
                    if not isinstance(function, dict):
                        raise protocol()
                    for key in ("name", "arguments"):
                        if key in function:
                            if not isinstance(function[key], str):
                                raise protocol()
                            call["function"][key] += function[key]
                finish = choice.get("finish_reason")
                if finish is not None:
                    if finish not in ("stop", "length", "tool_calls"):
                        raise protocol()
                    finished = finish

        try:
            async with asyncio.timeout(cfg["GenerateTimeoutSeconds"]):
                async with self.client.stream("POST", "/v1/chat/completions", json=chat_payload(cfg, prompt)) as response:
                    if response.status_code != 200:
                        body = bytearray()
                        async for block in response.aiter_bytes():
                            body.extend(block[:8192-len(body)])
                            if len(body) >= 8192:
                                break
                        code = engine_error(body.decode("utf-8", "replace"))
                        if response.status_code == 413 or code == "CONTEXT_TOO_LARGE":
                            raise ModelError("CONTEXT_TOO_LARGE", request_finished=True)
                        raise ModelError("MODEL_BUSY" if response.status_code in (429, 503) else "MODEL_UNAVAILABLE")
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise protocol()
                    pending, event = bytearray(), []
                    async for block in response.aiter_bytes():
                        total += len(block)
                        if total > cfg["MaxResponseBytes"]:
                            raise protocol()
                        pending.extend(block)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            line = line.rstrip(b"\r")
                            if line.startswith(b"data:"):
                                event.append(line[5:].lstrip(b" ").decode("utf-8"))
                            elif not line and event:
                                await consume("\n".join(event))
                                event = []
                            if done:
                                break
                        if done:
                            break
                    if not done or finished is None:
                        raise protocol()
        except (ValueError, UnicodeError, TypeError, KeyError) as error:
            raise protocol() from error
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ModelError("EXECUTION_TIMEOUT") from error
        except httpx.HTTPError as error:
            raise ModelError("MODEL_UNAVAILABLE") from error
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                raise protocol(True)
        accepted, ids = [], set()
        for index in sorted(calls) if finished != "length" else ():
            call = calls[index]
            if (not call["id"] or len(call["id"]) > 256 or call["id"] in ids or
                    call["function"]["name"] not in allowed):
                raise protocol(True)
            try:
                arguments = strict_json(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Object required")
            except (ValueError, TypeError) as error:
                raise protocol(True) from error
            ids.add(call["id"])
            accepted.append(call)
        if (len(accepted) > 1 and not prompt.parallel_tool_calls or
                prompt.tool_choice == "none" and accepted or
                finished == "tool_calls" and not accepted or
                finished == "stop" and accepted):
            raise protocol(True)
        if finished != "length" and prompt.tool_choice == "required" and not accepted:
            raise ModelError("TOOL_CHOICE_NOT_SATISFIED", request_finished=True)
        return Generation("".join(parts), finished != "length", finished,
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
            {"total_duration": (time.monotonic()-started)*1000},
            tuple(accepted) if finished != "length" else (), "".join(reasoning))


class ManagedStrataEngine(ManagedEngine):
    def launch_spec(self):
        cfg = self.cfg
        environment = os.environ.copy()
        environment.update(PYTHONUTF8="1", PYTHONNOUSERSITE="1", STRATA_KEEP_LEFTOVERS="1",
            PYTHONPATH=os.pathsep.join([str(path_of(cfg["DependencyPath"])), str(path_of(cfg["RuntimePath"]))]))
        environment.pop("STRATA_API_KEY", None)
        engine_dir = path_of(cfg["EnginePath"]).parent
        environment["PATH"] = str(engine_dir) + os.pathsep + environment.get("PATH", "")
        command = [str(self.executable), "-s", str(path_of(cfg["RuntimePath"]) / "serve/server.py"),
                   "--engine", "strata", "--config", str(path_of(cfg["RunConfigPath"])),
                   "--host", "127.0.0.1", "--port", str(urlsplit(cfg["BaseUrl"]).port)]
        return command, environment, path_of(cfg["RuntimePath"])

    async def start(self):
        launch = asyncio.create_task(asyncio.to_thread(self._launch))
        try:
            await asyncio.shield(launch)
        except asyncio.CancelledError:
            await launch
            await self.close()
            raise
        deadline = time.monotonic() + self.cfg["StartupTimeoutSeconds"]
        async with httpx.AsyncClient(trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.child.poll() is not None:
                    raise RuntimeError("Managed engine exited during startup")
                try:
                    response = await client.get(self.cfg["BaseUrl"] + "/health", timeout=2)
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.25)
        raise RuntimeError("Managed engine startup timed out")


class StrataProvider:
    supports_tools = False
    supports_thinking = False

    def __init__(self, settings, cfg, identity):
        self.cfg, self.identity = cfg, identity
        self.adapter = StrataAdapter(cfg)
        self.engine = ManagedStrataEngine(cfg, settings.data_directory / "logs")
        self.recovery_timeout_seconds = cfg["StartupTimeoutSeconds"] + 30

    async def verify_identity(self):
        try:
            props_response = await self.adapter.client.get("/props", timeout=10)
            slots_response = await self.adapter.client.get("/slots", timeout=10)
            props_response.raise_for_status()
            slots_response.raise_for_status()
            props, slots = props_response.json(), slots_response.json()
            if (props.get("model_alias") != self.cfg["Model"] or props.get("total_slots") != 1 or
                    props.get("build_info") != "Strata " + self.cfg["EngineVersion"] or props.get("is_sleeping") is not False or
                    props.get("default_generation_settings", {}).get("n_ctx") != self.cfg["NumCtx"] or
                    path_of(props["model_path"]) != path_of(identity_model_path(self.identity)) or
                    hashlib.sha256(props["chat_template"].encode()).hexdigest() != self.identity["chat_template_sha256"] or
                    not isinstance(slots, list) or len(slots) != 1 or slots[0].get("n_ctx") != self.cfg["NumCtx"] or
                    slots[0].get("is_processing") is not False):
                raise ValueError("Identity differs")
            self.supports_tools = props.get("chat_template_caps", {}).get("supports_tools") is True
            if not self.supports_tools:
                raise ValueError("Tools unavailable")
            self.supports_thinking = True
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise RuntimeError("MODEL_IDENTITY_CHECK_FAILED") from error

    async def start(self):
        await self.engine.start()
        await self.verify_identity()

    async def generate(self, prepared, stop, on_delta):
        if stop.is_set():
            raise asyncio.CancelledError
        raw = strict_json(prepared.payload_json)
        raw["image_sha256"] = tuple(raw["image_sha256"])
        prompt = PreparedPrompt(**raw)
        if prompt.material_sha256() != prepared.material_sha256:
            raise ModelError("MODEL_CONFIG_ERROR", request_finished=True)
        return await self.adapter.chat(prompt, on_delta)

    async def recover(self):
        # Transport close is not proof of GPU stop. Check the upstream queue and its only slot.
        try:
            for _ in range(20):
                responses = [await self.adapter.client.get(p, timeout=2) for p in ("/status", "/slots")]
                for response in responses:
                    response.raise_for_status()
                status, slots = [response.json() for response in responses]
                if (status.get("busy") is False and status.get("queued") == 0 and
                        isinstance(slots, list) and len(slots) == 1 and slots[0].get("is_processing") is False):
                    await self.verify_identity()
                    return True
                await asyncio.sleep(0.25)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, RuntimeError):
            pass
        try:
            await self.engine.restart()
            await self.verify_identity()
            return True
        except Exception:
            return False

    async def aclose(self):
        await self.adapter.aclose()
        await self.engine.close()

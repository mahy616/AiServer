"""The sole assembly point for the real provider and its immutable profile."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
import math

from aiserver.context.prompt_builder import PreparedPrompt, prepare_messages
from aiserver.context.input_budget import check_budget
from aiserver.context.window_budget import validate_window, freeze_output, budget_details, OutputLimitExceeded, max_output_tokens
from aiserver.inference.contracts import BudgetCheck, Generation, ModelError, PreparedInput, Profile
from aiserver.inference.ollama import ModelCallError, OllamaAdapter
from aiserver.operations.engine import ManagedEngine
from aiserver.settings import ROOT
from aiserver.inference.configuration import validate_options
from aiserver.inference.diagnostics import record


def load_profile(settings):
    cfg = json.loads(settings.inference_config.read_text(encoding="utf-8-sig"))
    validate_options(cfg)
    validate_window(cfg)
    # Old private profiles may retain MaxInputTokens=65536. It no longer caps
    # requests; capacity comes from the loaded window and each request's output.
    cfg.pop("MaxInputTokens", None)
    cfg["RequireFullGpu"] = True
    if (cfg.get("Model") == "qwen3.5:9b-q4_K_M" and
            cfg.get("Budget", {}).get("Method") != "ollama-render-tokenizer-v1"):
        raise ValueError("QWEN35_BUDGET_UPGRADE_REQUIRED")
    if cfg["NumParallel"] != 1 or cfg["MaxLoadedModels"] != 1 or cfg["EngineMaxQueue"] != 1:
        raise ValueError("This scheduler requires a single engine execution slot")
    lock = json.loads((ROOT / cfg["ModelLockPath"]).read_text(encoding="utf-8-sig"))
    snapshot = {"config": cfg, "identity": lock,
                "messageMappingVersion": "client-messages-v1",
                "capacityMethod": cfg.get("Budget", {}).get("Method", "conservative-estimate-v1")}
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    profile = Profile(cfg["ProfileId"] + "-" + hashlib.sha256(raw.encode()).hexdigest()[:12],
                      "Ollama", raw, ("text", "image") if "vision" in cfg["RequiredCapabilities"] else ("text",), False)
    return cfg, lock, profile


class PracticalBudget:
    """Bounded admission estimate, explicitly distinct from actual engine token counts.

    Mixed-language character weights and a padded patch-grid image allowance are
    intentionally conservative estimates, NOT a proof that a request fits.
    truncate=false/shift=false at the engine are the final no-data-loss boundary.
    """
    accepted_input_types = ("text", "image")

    def __init__(self, cfg, *, supports_thinking=False):
        self.cfg = cfg
        self.supports_thinking = supports_thinking
        self.counter = None
        if cfg.get("Budget", {}).get("Method") == "ollama-render-tokenizer-v1":
            from aiserver.context.coder_budget import CoderCounter
            self.counter = CoderCounter(cfg, ROOT)

    async def prepare(self, task, profile):
        try:
            prepared = await asyncio.to_thread(prepare_messages, json.loads(task.request_json),
                                               task.images, self.cfg["ImagePolicy"])
            prepared = freeze_output(prepared, self.cfg, supports_thinking=self.supports_thinking)
        except OutputLimitExceeded:
            return BudgetCheck(code="OUTPUT_LIMIT_EXCEEDED", details={"maxOutputTokens": max_output_tokens(self.cfg),
                                                                    "field": "max_tokens"})
        except ValueError as error:
            code = str(error)
            return BudgetCheck(code=code if code in ("IMAGE_INVALID", "IMAGE_LIMIT_EXCEEDED") else "INVALID_REQUEST")
        if self.counter:
            details = budget_details(self.cfg, prepared)
            try:
                count = await self.counter.count(prepared)
            except ModelCallError as error:
                return BudgetCheck(code=error.code, details=details if error.code == "CONTEXT_TOO_LARGE" else None)
            details["inputTokens"] = count
            record("admission", model=self.cfg["Model"], method="ollama-render-tokenizer-v1",
                   counted_tokens=count, input_limit=details["inputLimitTokens"],
                   output_budget=details["outputTokens"], accepted=count <= details["inputLimitTokens"])
            if count > details["inputLimitTokens"]:
                return BudgetCheck(code="CONTEXT_TOO_LARGE", details=details)
            return BudgetCheck(PreparedInput(profile.profile_id, json.dumps(asdict(prepared), ensure_ascii=False),
                                            prepared.content_sha256, "ollama-render-tokenizer-v1", prepared.prompt_version,
                                            estimated_input_tokens=count))
        decision = check_budget(prepared, self.cfg)
        record("admission", model=self.cfg["Model"], method=decision.method,
               counted_tokens=decision.estimated_input_tokens, input_limit=decision.limit_tokens,
               output_budget=json.loads(prepared.options_json)["num_predict"], accepted=decision.ready)
        if not decision.ready:
            return BudgetCheck(code=decision.code, details=budget_details(self.cfg, prepared, decision.estimated_input_tokens))
        return BudgetCheck(PreparedInput(profile.profile_id, json.dumps(asdict(prepared), ensure_ascii=False),
                                        prepared.content_sha256, "conservative-estimate-v1", prepared.prompt_version,
                                        estimated_input_tokens=decision.estimated_input_tokens))


class RealProvider:
    def __init__(self, settings, cfg, lock):
        self.cfg, self.lock = cfg, lock
        self.adapter = OllamaAdapter(cfg)
        self.engine = ManagedEngine(cfg, settings.data_directory / "logs") if settings.manage_engine else None
        self.logger = logging.getLogger("aiserver." + str(settings.data_directory))
        self.supports_tools = False
        self.supports_thinking = False

    async def start(self):
        if self.engine:
            await self.engine.start()
        await self.verify_identity()
        await self.verify_gpu()

    async def verify_gpu(self):
        row = await self.adapter.ensure_full_gpu()
        if row:
            self.logger.info("gpu_allocation_verified model=%s context=%s size=%s size_vram=%s",
                             self.cfg["Model"], row["context_length"], row["size"], row["size_vram"])

    async def verify_identity(self):
        client = self.adapter.client
        try:
            responses = [await client.get("/api/version", timeout=10), await client.get("/api/tags", timeout=10),
                         await client.post("/api/show", json={"model": self.cfg["Model"]}, timeout=10)]
            for response in responses:
                response.raise_for_status()
            version, tags, show = [r.json() for r in responses]
            model = next((m for m in tags["models"] if m["name"] == self.cfg["Model"]), None)
            expected = self.lock["model"]
            if (version["version"] != self.cfg["EngineVersion"] or not model
                    or model["digest"] != expected["digest"]
                    or show["details"]["quantization_level"] != self.cfg["ExpectedQuantization"]
                    or not set(self.cfg["RequiredCapabilities"]).issubset(show["capabilities"])
                    or hashlib.sha256(show["template"].encode()).hexdigest() != self.lock["template_sha256"]):
                raise ValueError("Model identity mismatch")
            self.supports_tools = "tools" in show["capabilities"]
            self.supports_thinking = "thinking" in show["capabilities"]
            from aiserver.inference.thinking import verify_thinking
            verify_thinking(self.cfg, show)
            if self.lock.get("yarn"):
                info = show.get("model_info", {})
                if any(info.get(k) != v for k, v in self.lock["yarn"].items()):
                    raise ValueError("YaRN model metadata mismatch")
                if info.get("qwen2.context_length", 0) < self.cfg["NumCtx"]:
                    raise ValueError("Requested context exceeds the installed model")
            self.logger.info("model_verified model=%s engine_version=%s quantization=%s capabilities=%s "
                             "requested_context=%s requested_batch=%s requested_kv=%s requested_flash=%s",
                             self.cfg["Model"], version["version"], self.cfg["ExpectedQuantization"],
                             ",".join(show["capabilities"]), self.cfg["NumCtx"], self.cfg.get("NumBatch", "engine-default"),
                             self.cfg.get("KvCacheType", "f16"), self.cfg.get("FlashAttention", "auto"))
        except Exception as error:
            raise RuntimeError("MODEL_IDENTITY_CHECK_FAILED") from error

    async def generate(self, prepared, stop, on_delta):
        if stop.is_set():
            raise asyncio.CancelledError
        raw = json.loads(prepared.payload_json)
        raw["image_sha256"] = tuple(raw["image_sha256"])
        prompt = PreparedPrompt(**raw)
        if prompt.material_sha256() != prepared.material_sha256:
            raise ModelError("MODEL_CONFIG_ERROR", request_finished=True)
        try:
            result = await self.adapter.chat(prompt, on_delta)
        except ModelCallError as error:
            record("generation_failed", model=self.cfg["Model"], code=error.code,
                   reason=error.reason or "transport_or_validation", request_finished=error.request_finished)
            raise ModelError(error.code, error.request_finished) from error
        actual_input = result.metrics.get("prompt_eval_count")
        record("usage_comparison", model=self.cfg["Model"], actual_input_tokens=actual_input,
               admission_tokens=prepared.estimated_input_tokens,
               delta_tokens=(actual_input-prepared.estimated_input_tokens)
               if type(actual_input) is int and type(prepared.estimated_input_tokens) is int else None)
        timings = {}
        for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
            value = result.metrics.get(key)
            if type(value) in (int,float) and math.isfinite(value) and value >= 0:
                timings[key] = value / 1000000
        return Generation(result.text, result.is_complete, result.finish_reason,
                          result.metrics.get("prompt_eval_count"), result.metrics.get("eval_count"), timings,
                          result.tool_calls, result.reasoning_text)

    async def recover(self):
        # Never infer GPU idleness from cancellation of a Python task.
        if not self.engine:
            return False
        try:
            await self.engine.restart()
            await self.verify_identity()
            await self.verify_gpu()
            return True
        except Exception as error:
            self.logger.error("engine_recovery_failed exception_type=%s", type(error).__name__)
            return False

    async def aclose(self):
        await self.adapter.aclose()
        if self.engine:
            await self.engine.close()

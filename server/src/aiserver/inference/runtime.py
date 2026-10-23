"""The sole assembly point for the real provider and its immutable profile."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path

from aiserver.context.prompt_builder import PreparedPrompt, prepare_prompt
from aiserver.context.input_budget import check_budget
from aiserver.inference.contracts import BudgetCheck, Generation, ModelError, PreparedInput, Profile
from aiserver.inference.ollama import ModelCallError, OllamaAdapter
from aiserver.operations.engine import ManagedEngine
from aiserver.settings import ROOT


def load_profile(settings):
    cfg = json.loads(settings.inference_config.read_text(encoding="utf-8-sig"))
    # Keep material and generation partitioning explicit; these are not tokenizer guarantees.
    if not (0 < cfg["MaxInputTokens"] <= cfg["NumCtx"] - cfg["NumPredict"] - cfg["ReservedTokens"]):
        raise ValueError("Invalid context/output partition")
    if cfg["NumParallel"] != 1 or cfg["MaxLoadedModels"] != 1 or cfg["EngineMaxQueue"] != 1:
        raise ValueError("This scheduler requires a single engine execution slot")
    lock = json.loads((ROOT / cfg["ModelLockPath"]).read_text(encoding="utf-8-sig"))
    prompt = settings.system_prompt.read_text(encoding="utf-8-sig")
    snapshot = {"config": cfg, "identity": lock,
                "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "capacityMethod": "conservative-estimate-v1"}
    raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    profile = Profile(cfg["ProfileId"] + "-" + hashlib.sha256(raw.encode()).hexdigest()[:12],
                      "Ollama", raw, ("text", "image"), False)
    return cfg, lock, prompt, profile


class PracticalBudget:
    """Bounded admission estimate, explicitly distinct from actual engine token counts.

    Mixed-language character weights and a padded patch-grid image allowance are
    intentionally conservative estimates, NOT a proof that a request fits.
    truncate=false/shift=false at the engine are the final no-data-loss boundary.
    """
    accepted_input_types = ("text", "image")

    def __init__(self, cfg, prompt_path):
        self.cfg, self.prompt_path = cfg, prompt_path

    async def prepare(self, task, profile):
        try:
            prepared = await asyncio.to_thread(prepare_prompt, json.loads(task.request_json), self.prompt_path,
                                               tuple(x.png for x in task.images), self.cfg["ImagePolicy"])
        except ValueError as error:
            code = str(error)
            return BudgetCheck(code=code if code in ("IMAGE_INVALID", "IMAGE_LIMIT_EXCEEDED") else "INVALID_REQUEST")
        # Hash-bound prompt prevents a file edit from silently changing queued materials.
        current_hash = hashlib.sha256(prepared.messages()[0]["content"].encode("utf-8")).hexdigest()
        if current_hash != json.loads(profile.snapshot_json)["promptSha256"]:
            return BudgetCheck(code="MODEL_CONFIG_ERROR")
        decision = check_budget(prepared, self.cfg)
        if not decision.ready:
            return BudgetCheck(code=decision.code)
        return BudgetCheck(PreparedInput(profile.profile_id, json.dumps(asdict(prepared), ensure_ascii=False),
                                        prepared.content_sha256, "conservative-estimate-v1", prepared.prompt_version,
                                        estimated_input_tokens=decision.estimated_input_tokens))


class RealProvider:
    def __init__(self, settings, cfg, lock):
        self.cfg, self.lock = cfg, lock
        self.adapter = OllamaAdapter(cfg)
        self.engine = ManagedEngine(cfg, settings.data_directory / "logs") if settings.manage_engine else None
        self.logger = logging.getLogger("aiserver." + str(settings.data_directory))

    async def start(self):
        if self.engine:
            await self.engine.start()
        await self.verify_identity()

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
        except Exception as error:
            raise RuntimeError("MODEL_IDENTITY_CHECK_FAILED") from error

    async def generate(self, prepared, stop):
        if stop.is_set():
            raise asyncio.CancelledError
        raw = json.loads(prepared.payload_json)
        raw["image_sha256"] = tuple(raw["image_sha256"])
        prompt = PreparedPrompt(**raw)
        if hashlib.sha256(prompt.messages_json.encode()).hexdigest() != prepared.material_sha256:
            raise ModelError("MODEL_CONFIG_ERROR", request_finished=True)
        try:
            result = await self.adapter.chat(prompt)
        except ModelCallError as error:
            raise ModelError(error.code, error.request_finished) from error
        return Generation(result.text, result.is_complete, result.finish_reason,
                          result.metrics.get("prompt_eval_count"), result.metrics.get("eval_count"))

    async def recover(self):
        # Never infer GPU idleness from cancellation of a Python task.
        if not self.engine:
            return False
        try:
            await self.engine.restart()
            await self.verify_identity()
            return True
        except Exception as error:
            self.logger.error("engine_recovery_failed exception_type=%s", type(error).__name__)
            return False

    async def aclose(self):
        await self.adapter.aclose()
        if self.engine:
            await self.engine.close()

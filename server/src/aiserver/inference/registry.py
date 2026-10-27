"""Several locked models on ONE Ollama process and ONE scheduler execution slot."""
from pathlib import Path
import asyncio
import time

from aiserver.inference.runtime import load_profile, PracticalBudget, RealProvider
from aiserver.inference.configuration import process_signature, PROCESS_KEYS
from aiserver.inference.diagnostics import record
from aiserver.inference.contracts import BudgetCheck, ModelError


def load_models(settings):
    """Read and validate every profile before allocating any transports/processes."""
    entries, models, engine_cfg = [], set(), None
    for index, path in enumerate([settings.inference_config, *settings.additional_inference_configs]):
        current = settings.model_copy(update={"inference_config": Path(path),
                                              "manage_engine": settings.manage_engine and index == 0})
        cfg, identity, profile = load_profile(current)
        signature = process_signature(cfg)
        # KV precision and attention are process-wide Ollama options. An owned
        # engine can restart between jobs; an external engine cannot be changed.
        process = {key: signature[key] for key in PROCESS_KEYS} if settings.manage_engine else signature
        if engine_cfg is not None and process != engine_cfg:
            raise ValueError("Models must share the same Ollama process configuration")
        if cfg["Model"] in models:
            raise ValueError("Duplicate model id")
        engine_cfg = process
        models.add(cfg["Model"])
        entries.append((current, cfg, identity, profile))
    return entries


class ModelBackends:
    def __init__(self, settings):
        self.bindings = {}
        self.engine_gate = asyncio.Lock()
        self.needs_recovery = False
        self.process_settings_verified = True
        for current, cfg, identity, profile in load_models(settings):
            self.bindings[profile.profile_id] = (profile, PracticalBudget(cfg), RealProvider(current, cfg, identity))
        self.profiles = [entry[0] for entry in self.bindings.values()]
        self.accepted_input_types = tuple(dict.fromkeys(t for p in self.profiles for t in p.accepted_input_types))
        self.active = next(iter(self.bindings.values()))[2]
        self.engine = self.active.engine

    def tools_supported(self, profile):
        return self.bindings[profile.profile_id][2].supports_tools

    def thinking_supported(self, profile):
        return self.bindings[profile.profile_id][2].supports_thinking

    async def prepare(self, task, profile):
        _, budget, provider = self.bindings[profile.profile_id]
        budget.supports_thinking = provider.supports_thinking
        if budget.counter is None:
            return await budget.prepare(task, profile)
        # Rendering may load a runner. Never count another model while a
        # generation owns the slot, and apply target KV/FA BEFORE rendering.
        waiting = time.monotonic()
        async with self.engine_gate:
            record("engine_gate", operation="admission", wait_ms=round((time.monotonic()-waiting)*1000, 2))
            if self.needs_recovery:
                return BudgetCheck(code="ENGINE_RECOVERING")
            try:
                await self.activate(provider)
            except Exception as error:
                record("model_switch_failed", model=provider.cfg["Model"], exception_type=type(error).__name__)
                return BudgetCheck(code="MODEL_UNAVAILABLE")
            return await budget.prepare(task, profile)

    async def activate(self, provider):
        self.active = provider
        if self.engine and (not self.process_settings_verified or
                            process_signature(self.engine.cfg) != process_signature(provider.cfg)):
            record("model_switch", model=provider.cfg["Model"], reason="process_settings_changed",
                   previous_kv=self.engine.cfg.get("KvCacheType", "f16"),
                   kv=provider.cfg.get("KvCacheType", "f16"), flash=provider.cfg.get("FlashAttention"))
            self.engine.cfg = provider.cfg
            self.process_settings_verified = False
            await self.restart()
            await provider.verify_identity()
            self.process_settings_verified = True

    async def restart(self):
        # _launch runs in a thread. Keep the gate until it settles, including
        # cancellation, so recovery cannot race a still-launching process.
        task = asyncio.create_task(self.engine.restart())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def generate(self, prepared, stop, on_delta):
        provider = self.bindings[prepared.profile_id][2]
        async with self.engine_gate:
            if self.needs_recovery:
                raise ModelError("MODEL_UNAVAILABLE")
            try:
                await self.activate(provider)
                return await provider.generate(prepared, stop, on_delta)
            except ModelError as error:
                self.needs_recovery = not error.request_finished
                raise
            except BaseException:
                self.needs_recovery = True
                raise

    async def start(self):
        if self.engine:
            await self.engine.start()
        for _, _, provider in self.bindings.values():
            await provider.verify_identity()
        for _, _, provider in self.bindings.values():
            await self.activate(provider)
            await provider.verify_gpu()
        await self.activate(next(iter(self.bindings.values()))[2])
        if len(self.bindings) > 1:
            await self.active.verify_gpu()

    async def recover(self):
        if not self.engine:
            return False
        try:
            async with self.engine_gate:
                record("recovery", model=self.active.cfg["Model"], reason="restart_requested")
                await self.restart()
                for _, _, provider in self.bindings.values():
                    await provider.verify_identity()
                await self.active.verify_gpu()
                self.needs_recovery = False
                self.process_settings_verified = True
            return True
        except Exception as error:
            record("recovery", code="ENGINE_RECOVERY_FAILED", exception_type=type(error).__name__)
            return False

    async def aclose(self):
        for _, _, provider in reversed(list(self.bindings.values())):
            await provider.aclose()

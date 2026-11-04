"""Publish several Strata profiles, loading one model only when its FIFO job runs."""
import asyncio
from pathlib import Path

from aiserver.inference.contracts import BudgetCheck, ModelError
from aiserver.inference.diagnostics import record
from aiserver.inference.strata import load_strata_profile, path_of, StrataBudget, StrataProvider


def load_strata_models(settings):
    entries, models, profiles, base_url = [], set(), set(), None
    for path in [settings.inference_config, *settings.additional_inference_configs]:
        current = settings.model_copy(update={"inference_config": Path(path)})
        cfg, identity, profile = load_strata_profile(current)
        if cfg["Model"] in models or profile.profile_id in profiles:
            raise ValueError("Duplicate Strata model/profile id")
        if base_url is not None and cfg["BaseUrl"] != base_url:
            raise ValueError("Strata models must share one managed loopback endpoint")
        models.add(cfg["Model"])
        profiles.add(profile.profile_id)
        base_url = cfg["BaseUrl"]
        entries.append((current, cfg, identity, profile))
    return entries


class StrataBackends:
    accepted_input_types = ("text",)

    def __init__(self, settings):
        self.settings = settings
        self.bindings = {}
        self.engine_gate = asyncio.Lock()
        self.active = None
        self.loaded = self.needs_recovery = False
        # Validate ALL local identities before allocating transports or starting a model.
        for current, cfg, identity, profile in load_strata_models(settings):
            self.bindings[profile.profile_id] = (profile, StrataBudget(cfg), StrataProvider(current, cfg, identity))
        self.profiles = [entry[0] for entry in self.bindings.values()]
        self.default = next(iter(self.bindings.values()))[2]
        self.engine = self.default.engine
        for _, _, provider in self.bindings.values():
            provider.engine = self.engine
        self.recovery_timeout_seconds = max(p.cfg["StartupTimeoutSeconds"] for _, _, p in self.bindings.values()) + 30

    def tools_supported(self, profile):
        # The pinned, locally validated Qwen/DeepSeek profiles both support tools
        # and boolean thinking. Runtime identity/template checks still precede generation.
        return profile.profile_id in self.bindings

    def thinking_supported(self, profile):
        return profile.profile_id in self.bindings

    async def prepare(self, task, profile):
        if self.needs_recovery:
            return BudgetCheck(code="ENGINE_RECOVERING")
        return await self.bindings[profile.profile_id][1].prepare(task, profile)

    def execution_timeout(self, prepared):
        provider = self.bindings[prepared.profile_id][2]
        loading = provider.cfg["StartupTimeoutSeconds"] + 30 if not self.loaded or self.active is not provider else 0
        return self.settings.execution_timeout_seconds + loading

    async def activate(self, provider):
        if self.loaded and self.active is provider:
            return
        self.loaded = False
        try:
            # Keep the single OS ownership lock across the unload/load boundary.
            # Do not replace executable/config until the previous tree has stopped.
            await self.engine.unload()
            self.active = provider
            self.engine.cfg = provider.cfg
            self.engine.executable = path_of(provider.cfg["ExecutablePath"])
            record("model_switch", model=provider.cfg["Model"], reason="strata_job")
            await self.engine.start()
            await provider.verify_identity()
            self.loaded = True
        except BaseException as error:
            self.needs_recovery = True
            try:
                await self.engine.unload()
            except BaseException:
                # Retain the recovery gate if tree shutdown cannot be confirmed.
                raise
            self.active = None
            self.needs_recovery = False
            if isinstance(error, asyncio.CancelledError):
                raise
            record("model_switch_failed", model=provider.cfg["Model"], exception_type=type(error).__name__)
            raise ModelError("MODEL_UNAVAILABLE", request_finished=True) from error

    async def start(self):
        async with self.engine_gate:
            await self.activate(self.default)

    async def generate(self, prepared, stop, on_delta):
        provider = self.bindings[prepared.profile_id][2]
        async with self.engine_gate:
            if self.needs_recovery:
                raise ModelError("MODEL_UNAVAILABLE")
            try:
                if stop.is_set():
                    raise asyncio.CancelledError
                await self.activate(provider)
                return await provider.generate(prepared, stop, on_delta)
            except ModelError as error:
                self.needs_recovery = not error.request_finished
                raise
            except BaseException:
                self.needs_recovery = True
                raise

    async def recover(self):
        try:
            async with self.engine_gate:
                if self.loaded and self.active is not None:
                    if not await self.active.recover():
                        return False
                else:
                    await self.engine.unload()
                    self.active = None
                self.needs_recovery = False
                return True
        except Exception:
            return False

    async def aclose(self):
        async with self.engine_gate:
            self.loaded = False
            try:
                await self.engine.unload()
            finally:
                try:
                    await self.engine.close()
                finally:
                    for _, _, provider in self.bindings.values():
                        await provider.adapter.aclose()

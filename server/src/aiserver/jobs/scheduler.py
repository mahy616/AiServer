import asyncio
from dataclasses import asdict
import json
import sqlite3
import time
import logging

from aiserver.api.errors import ApiError
from aiserver.api.models import model_description
from aiserver.api.validation import request_text_bytes
from aiserver.inference.contracts import ModelError, PreparedInput
from aiserver.inference.diagnostics import inference_context, record

class Scheduler:
    def __init__(self, store, settings, profile, budget, provider, profiles=None):
        self.store, self.settings, self.profile = store, settings, profile
        self.budget, self.provider = budget, provider
        self.profiles = {model_description(p, settings)["id"]: p for p in (profiles or [profile])}
        self.accepted_input_types = tuple(dict.fromkeys(x for p in self.profiles.values()
                                          for x in p.accepted_input_types if x in budget.accepted_input_types))
        self.gate = asyncio.Lock()
        self.wake = asyncio.Event()
        self.state = "starting"
        self.stopping = False
        self.fatal = False
        self.fatal_code = "SERVICE_NOT_READY"
        self.active_job = None
        self.worker = None
        self.orphans = set()

    async def db(self, method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except sqlite3.Error:
            self.fatal_code = "STORAGE_UNAVAILABLE"
            raise

    async def start(self):
        await self.db(self.store.startup)
        await self.db(self.store.cleanup)
        recovery = await self.db(self.store.recovery)
        self.state = "recovering" if recovery["recovery_required"] else "available"
        if recovery["recovery_required"] and hasattr(self.provider, "recover"):
            await self.recover_engine(recovery["recovery_job_id"])
        self.worker = asyncio.create_task(self.run(), name="persistent-job-consumer")

    async def can_accept(self):
        if self.fatal:
            return False, self.fatal_code
        if self.stopping or self.fatal or self.state in ("starting", "paused", "stopping"):
            return False, "SERVICE_NOT_READY"
        state = await self.db(self.store.recovery)
        if state["recovery_required"] and not (
            self.active_job == state["recovery_job_id"] and state["recovery_reason"] == "DRAINING"):
            return False, "ENGINE_RECOVERING"
        if not self.accepted_input_types:
            return False, "INPUT_BUDGET_UNVERIFIED"
        return True, None

    async def submit(self, user, request, fingerprint, task, received_at_ms=None):
        existing = await self.db(self.store.replay, user, request["clientRequestId"], fingerprint)
        if existing:
            return existing, True
        profile = self.profiles.get(request["model"])
        if profile is None:
            raise ApiError(404, "MODEL_NOT_FOUND")
        model = model_description(profile, self.settings)
        from aiserver.inference.thinking import resolve_thinking, ThinkingParameterError
        thinking_supported = (self.provider.thinking_supported(profile)
            if hasattr(self.provider, "thinking_supported") else getattr(self.provider, "supports_thinking", False))
        try:
            resolve_thinking(json.loads(profile.snapshot_json).get("config", {}), request.get("think"), thinking_supported)
        except ThinkingParameterError as error:
            raise ApiError(400, "UNSUPPORTED_PARAMETER", {"field": "think", "reason": str(error)}) from None
        if request_text_bytes(request) > model["limits"]["maxTextUtf8Bytes"]:
            raise ApiError(413, "REQUEST_TOO_LARGE")
        if request.get("tools"):
            supported = (self.provider.tools_supported(profile) if hasattr(self.provider, "tools_supported")
                         else getattr(self.provider, "supports_tools", False))
            if not supported:
                raise ApiError(400, "TOOLS_NOT_SUPPORTED")
        if len(task.images) > model["limits"]["maxImages"]:
            raise ApiError(422, "IMAGE_LIMIT_EXCEEDED")
        ready, reason = await self.can_accept()
        if not ready:
            raise ApiError(503, reason)
        needed = "image" if task.images else "text"
        if needed not in profile.accepted_input_types or needed not in self.budget.accepted_input_types:
            raise ApiError(503, "INPUT_BUDGET_UNVERIFIED")
        with inference_context(request_id=request["clientRequestId"], model=request["model"],
                               logger_name="aiserver." + str(self.settings.data_directory)):
            check = await self.budget.prepare(task, profile)
            if check.code:
                record("admission_rejected", code=check.code)
        if check.code:
            raise ApiError(400 if check.code == "OUTPUT_LIMIT_EXCEEDED" else
                           422 if check.code in ("CONTEXT_TOO_LARGE", "IMAGE_LIMIT_EXCEEDED") else 503,
                           check.code, check.details)
        prepared = check.prepared
        if prepared is None or prepared.profile_id != profile.profile_id:
            raise ApiError(503, "SERVICE_NOT_READY")
        effective = {"profile":asdict(profile), "prepared":asdict(prepared),
                     "images":[{"imageId":x.image_id,"receivedSha256":x.sha256,
                                "sentSha256":x.sha256,"width":x.width,"height":x.height,
                                "mediaType":x.media_type,"processingVersion":"pass-through-image-v1"} for x in task.images]}
        async with self.gate:
            existing = await self.db(self.store.replay,user,request["clientRequestId"],fingerprint)
            if existing:
                return existing, True
            ready, reason = await self.can_accept()
            if not ready:
                raise ApiError(503, reason)
            result = await self.db(self.store.insert,user,request,fingerprint,effective,self.active_job is not None,received_at_ms)
        self.wake.set()
        return result

    async def cancel(self, user, job, reason="user_cancel"):
        async with self.gate:
            result = await self.db(self.store.cancel,user,job,reason)
        self.wake.set()
        return result

    async def run(self):
        last_cleanup = time.monotonic()
        try:
            while not self.stopping:
                await self.db(self.store.expire_waiting)
                if time.monotonic()-last_cleanup >= 3600:
                    await self.db(self.store.cleanup)
                    last_cleanup = time.monotonic()
                row = None
                async with self.gate:
                    if not self.stopping and self.state not in ("recovering","paused"):
                        row = await self.db(self.store.claim)
                        if row:
                            self.active_job = row["job_id"]
                            self.state = "busy"
                if row:
                    await self.execute(row)
                    continue
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except TimeoutError:
                    pass
        except Exception:
            # No payload/exception text enters logs or responses.
            self.fatal = True
            self.state = "paused"
            logging.getLogger("aiserver." + str(self.settings.data_directory)).error("worker_paused code=%s", self.fatal_code)

    async def save(self, method, *args):
        for attempt in range(3):
            try:
                return await self.db(method,*args)
            except sqlite3.Error:
                if attempt == 2:
                    self.state = "paused"
                    raise
                await asyncio.sleep(1)

    async def recover_engine(self, job):
        self.state = "recovering"
        try:
            async with asyncio.timeout(getattr(self.provider, "recovery_timeout_seconds", self.settings.cleanup_timeout_seconds)):
                restored = await self.provider.recover()
            if restored:
                await self.db(self.store.engine_recovered, job)
                self.state = "available"
                return True
        except Exception:
            pass
        return False

    async def execute(self, row):
        job = row["job_id"]
        effective = json.loads(row["effective_settings_json"])
        profile = next((p for p in self.profiles.values() if p.profile_id == row["profile_id"]), None)
        if profile is None or effective["profile"]["snapshot_json"] != profile.snapshot_json:
            await self.save(self.store.finish,job,None,"MODEL_CONFIG_ERROR")
            self.state, self.active_job = "paused", None
            return
        prepared = PreparedInput(**effective["prepared"])
        stop = asyncio.Event()
        async def on_delta(text, channel="content"):
            await self.save(self.store.append_output, job, text, channel)
        with inference_context(job_id=job, profile_id=profile.profile_id,
                               budget_method=prepared.budget_method,
                               admission_tokens=prepared.estimated_input_tokens,
                               logger_name="aiserver." + str(self.settings.data_directory)):
            generation = asyncio.create_task(self.provider.generate(prepared,stop,on_delta))
        deadline = time.monotonic()+self.settings.execution_timeout_seconds
        cleanup_needed = False
        last_queue_check = time.monotonic()
        try:
            while True:
                if time.monotonic() - last_queue_check >= 1:
                    await self.db(self.store.expire_waiting)
                    last_queue_check = time.monotonic()
                current = await self.db(self.store.by_job,job)
                reason = None
                if current["status"] == "cancelling":
                    reason = "user_cancel"
                elif self.stopping:
                    reason = "SERVICE_STOPPED"
                elif time.monotonic() >= deadline:
                    reason = "EXECUTION_TIMEOUT"
                if reason:
                    await self.save(self.store.stop_running,job,reason)
                    cleanup_needed = True
                    break
                if generation.done():
                    try:
                        output = generation.result()
                        await self.save(self.store.finish,job,output)
                    except ModelError as error:
                        await self.save(self.store.finish,job,None,error.code)
                        if not error.request_finished:
                            await self.db(self.store.mark_recovery,job,error.code)
                            self.state = "recovering"
                            if hasattr(self.provider, "recover"):
                                await self.recover_engine(job)
                    except Exception:
                        await self.save(self.store.finish,job,None,"MODEL_PROTOCOL_ERROR")
                        await self.db(self.store.mark_recovery,job,"MODEL_PROTOCOL_ERROR")
                        self.state = "recovering"
                        if hasattr(self.provider, "recover"):
                            await self.recover_engine(job)
                    # finish may have lost a race to an accepted cancel.
                    current = await self.db(self.store.by_job,job)
                    if current["status"] == "cancelling":
                        await self.save(self.store.stop_running,job,"user_cancel")
                        cleanup_needed = True
                    break
                await asyncio.wait({generation}, timeout=0.05)
            if cleanup_needed:
                self.state = "draining"
                stop.set()
                generation.cancel()
                done, _ = await asyncio.wait({generation},timeout=self.settings.cleanup_timeout_seconds)
                if done:
                    # Consume/discard any late result or exception; never persist it.
                    try:
                        generation.result()
                    except BaseException:
                        pass
                if done and profile.automatic_cleanup_verified:
                    await self.db(self.store.clear_cleanup,job)
                    self.state = "available"
                elif done and hasattr(self.provider, "recover") and await self.recover_engine(job):
                    pass
                else:
                    await self.db(self.store.mark_recovery,job,"CLEANUP_UNCONFIRMED")
                    self.state = "recovering"
                    if not done:
                        self.orphans.add(generation)
                        generation.add_done_callback(self.consume_orphan)
        finally:
            if not generation.done():
                generation.cancel()
                done, _ = await asyncio.wait({generation}, timeout=1)
                if done:
                    self.consume_orphan(generation)
                else:
                    self.orphans.add(generation)
                    generation.add_done_callback(self.consume_orphan)
            logging.getLogger("aiserver." + str(self.settings.data_directory)).info(
                "job_finished job=%s scheduler=%s", job, self.state)
            if self.state == "busy":
                self.state = "available"
            self.active_job = None

    def consume_orphan(self, task):
        self.orphans.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    async def close(self):
        async with self.gate:
            self.stopping = True
        self.wake.set()
        if self.worker:
            await self.worker
        await self.db(self.store.shutdown_waiting)
        self.state = "stopping"
        await self.provider.aclose()

import asyncio
from contextlib import asynccontextmanager
import sqlite3
import json
import time
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from aiserver.api.errors import ApiError, safe_error
from aiserver.api.presentation import available, iso, result_response, status_response
from aiserver.api.validation import RequestValidator
from aiserver.api.body import read_json_body
from aiserver.api.models import model_description
from aiserver.api.streaming import InferenceStream, TraceHeadersMiddleware, event
from aiserver.api.openai import register_openai, error_body, error_status
from aiserver.inference.fake import FakeBudget, FakeProvider, fake_profile
from aiserver.jobs.scheduler import Scheduler
from aiserver.operations.lock import InstanceLock
from aiserver.operations.observability import RateLimits, service_logger
from aiserver.operations.diagnostics import diagnostic
from aiserver.operations.monitoring import Monitor
from aiserver.settings import Settings
from aiserver.storage.db import Store, TERMINAL, now_ms


def json_capacity_method(profile):
    snapshot = json.loads(profile.snapshot_json)
    return snapshot.get("capacityMethod", snapshot.get("budgetMethod", "unknown"))

def create_app(settings=None, *, profile=None, budget=None, provider=None):
    settings = settings or Settings.load()
    store = Store(settings)
    profiles = None
    if settings.provider == "Strata" and profile is None and budget is None and provider is None:
        from aiserver.inference.strata_registry import StrataBackends
        provider = budget = StrataBackends(settings)
        profiles = provider.profiles
        profile = profiles[0]
    elif settings.provider == "Ollama" and profile is None and budget is None and provider is None:
        if settings.additional_inference_configs:
            from aiserver.inference.registry import ModelBackends
            provider = budget = ModelBackends(settings)
            profiles = provider.profiles
            profile = profiles[0]
        else:
            from aiserver.inference.runtime import load_profile, PracticalBudget, RealProvider
            cfg, identity, profile = load_profile(settings)
            budget = PracticalBudget(cfg)
            provider = RealProvider(settings, cfg, identity)
    else:
        profile = profile or fake_profile(settings)
        budget = budget or FakeBudget(settings.fake_max_material_bytes)
        provider = provider or FakeProvider(settings.fake_delay_seconds,settings.fake_scenario,settings.fake_late_seconds)
    scheduler = Scheduler(store,settings,profile,budget,provider,profiles)
    monitor = Monitor(settings,store,scheduler)
    validator = RequestValidator(settings.contract_directory)
    limits = RateLimits()

    @asynccontextmanager
    async def lifespan(app):
        lock = InstanceLock(settings.data_directory)
        logger = handler = None
        phase, acquired, started = "data-lock", False, False
        try:
            lock.acquire()
            acquired = True
            phase = "logging"
            logger, handler = service_logger(settings)
            app.state.logger = logger
            logger.addHandler(monitor.handler)
            phase = "storage"
            await asyncio.to_thread(store.initialize)
            phase = "engine"
            if hasattr(provider, "start"):
                await provider.start()
            phase = "scheduler"
            await scheduler.start()
            started = True
            monitor.start()
            logger.info("service_started provider=%s", profile.provider)
            try:
                yield
            finally:
                await scheduler.close()
        except Exception as error:
            if not started:
                app.state.startup_failure = diagnostic(error,phase)
                if logger:
                    logger.error("startup_failed phase=%s code=%s type=%s",phase,
                                 app.state.startup_failure["code"],type(error).__name__)
            raise
        finally:
            try:
                if acquired:
                    await provider.aclose()
            finally:
                if logger:
                    await monitor.close()
                    logger.info("service_stopped")
                    logger.removeHandler(monitor.handler)
                    logger.removeHandler(handler)
                    handler.close()
                lock.release()

    app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=settings.allowed_hosts)
    app.state.store, app.state.scheduler, app.state.settings = store, scheduler, settings
    app.state.monitor = monitor
    app.add_middleware(TraceHeadersMiddleware, state=app.state)

    def error_response(request,error):
        headers = {"Retry-After":str(error.details.get("retryAfterSeconds", settings.poll_seconds))} if error.status in (429,503) else {}
        if request.url.path.startswith("/v1/"):
            return JSONResponse(error_body(error), status_code=error_status(error), headers=headers)
        return JSONResponse({"error":safe_error(error.code,error.details),
                             "traceId":getattr(request.state,"trace_id",str(uuid4()))},
                            status_code=error.status,headers=headers)

    @app.exception_handler(ApiError)
    async def api_error(request,error):
        return error_response(request,error)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request,error):
        scheduler.fatal, scheduler.state = True, "paused"
        return error_response(request,ApiError(503,"STORAGE_UNAVAILABLE"))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request,error):
        return error_response(request,ApiError(400,"INVALID_REQUEST"))

    @app.exception_handler(HTTPException)
    async def http_error(request,error):
        return error_response(request,ApiError(error.status_code,"INVALID_REQUEST"))

    @app.exception_handler(Exception)
    async def internal(request,error):
        return error_response(request,ApiError(500,"INTERNAL_ERROR"))

    async def user(request: Request):
        value = request.headers.get("authorization","")
        if not value.startswith("Bearer ") or len(value)>512:
            if not limits.allow("auth_failed", settings.public_requests_per_minute):
                raise ApiError(429, "RATE_LIMITED")
            raise ApiError(401,"UNAUTHORIZED")
        try:
            details = await asyncio.to_thread(store.authenticate_details,value[7:])
        except ApiError:
            if not limits.allow("auth_failed", settings.public_requests_per_minute):
                raise ApiError(429, "RATE_LIMITED")
            raise
        identity = details["userId"]
        monitor.seen(identity)
        request.state.identity = details
        if not limits.allow((identity, "requests"), settings.requests_per_minute):
            raise ApiError(429, "RATE_LIMITED")
        if request.method == "POST" and request.url.path in ("/api/v1/jobs", "/v1/chat/completions") and not limits.allow(
                (identity, "submit"), settings.submissions_per_minute):
            raise ApiError(429, "RATE_LIMITED")
        return identity

    def uuid(value):
        try:
            return str(UUID(value))
        except ValueError:
            raise ApiError(400,"INVALID_REQUEST") from None

    async def state(row):
        queue = None
        if row["status"] == "queued":
            waiting, active = await scheduler.db(store.queue,row["sequence"])
            paused = scheduler.state in ("draining","recovering","paused","stopping")
            queue = {"waitingAhead":waiting,"activeAhead":active,"position":waiting+1,"isPaused":paused}
        return status_response(row,scheduler.state,queue,settings.poll_seconds)

    @app.get("/api/v1/auth/me")
    async def me(request: Request, user_id=Depends(user)):
        identity = request.state.identity
        return {"user":{key:identity[key] for key in ("userId", "displayName")},
                "expiresAt":iso(identity["expiresAtMs"])}

    @app.get("/api/v1/health")
    async def health():
        if not limits.allow("health", settings.public_requests_per_minute):
            raise ApiError(429, "RATE_LIMITED")
        if scheduler.fatal:
            raise ApiError(503,"SERVICE_NOT_READY")
        return {"status":"ok"}

    @app.get("/api/v1/ready")
    async def ready(user_id=Depends(user)):
        can_accept, reason = await scheduler.can_accept()
        return JSONResponse({"canAcceptJobs":can_accept,"schedulerState":scheduler.state,
                             "reasonCode":reason,"observedAt":iso(now_ms()),
                             "recommendedPollSeconds":settings.poll_seconds,
                             "inferenceProvider":profile.provider,
                             "acceptedInputTypes":list(scheduler.accepted_input_types),
                             "capacityMethod":json_capacity_method(profile),
                             "storage":"ok" if not scheduler.fatal else "unavailable"},
                            status_code=200 if can_accept else 503)

    @app.get("/api/v1/models")
    async def models(user_id=Depends(user)):
        descriptions = []
        for profile in scheduler.profiles.values():
            description = model_description(profile, settings)
            description["toolCall"] = bool(scheduler.provider.tools_supported(profile)
                if hasattr(scheduler.provider, "tools_supported") else getattr(scheduler.provider, "supports_tools", False))
            description["thinking"] = bool(scheduler.provider.thinking_supported(profile)
                if hasattr(scheduler.provider, "thinking_supported") else getattr(scheduler.provider, "supports_thinking", False))
            if not description["thinking"]:
                for key in ("thinkingLevels", "defaultThinkingLevel", "thinkingCanDisable"):
                    description.pop(key, None)
            description["inputTypes"] = [t for t in description["inputTypes"] if t in budget.accepted_input_types]
            descriptions.append(description)
        return {"models": descriptions}

    @app.post("/api/v1/jobs")
    async def submit(request: Request, user_id=Depends(user)):
        body = await read_json_body(request, settings.max_request_bytes, settings.upload_timeout_seconds)
        normalized,fingerprint,task = await asyncio.to_thread(validator.validate,bytes(body))
        row,replayed = await scheduler.submit(user_id,normalized,fingerprint,task,request.state.received_at_ms)
        if replayed:
            raise ApiError(409, "REQUEST_ALREADY_EXISTS", {"jobId": row["job_id"], "status": row["status"]})
        job = row["job_id"]

        async def body_events():
            yield event("accepted", {"jobId": job, "clientRequestId": row["client_request_id"],
                                     "model": normalized["model"]})
            sequence, previous, last_status = 0, None, 0
            heartbeat = time.monotonic()
            try:
                while True:
                    # Read terminal status BEFORE chunks: never omit the last committed delta.
                    current = await scheduler.db(store.get, user_id, job)
                    chunks = await scheduler.db(store.chunks, job, sequence)
                    for chunk in chunks:
                        sequence = chunk["sequence"]
                        kind = "reasoning" if chunk["channel"] == "reasoning_content" else "delta"
                        yield event(kind, {"jobId": job, "sequence": sequence, "text": chunk["text"]})
                    observed = await state(current)
                    signature = (current["status"], scheduler.state, json.dumps(observed["queue"], sort_keys=True))
                    if signature != previous or time.monotonic()-last_status >= settings.poll_seconds:
                        yield event("status", observed)
                        previous, last_status = signature, time.monotonic()
                    if current["status"] in TERMINAL:
                        if len(chunks) == 64:
                            continue
                        yield event({"succeeded":"completed", "failed":"failed", "cancelled":"cancelled"}[current["status"]],
                                    result_response(current))
                        return
                    if time.monotonic()-heartbeat >= settings.stream_heartbeat_seconds:
                        yield b": keep-alive\n\n"
                        heartbeat = time.monotonic()
                    await asyncio.sleep(0.05)
            except (sqlite3.Error, ApiError):
                scheduler.fatal, scheduler.state = True, "paused"
                yield event("error", {"jobId": job, "error": safe_error("STORAGE_UNAVAILABLE"),
                                      "traceId": request.state.trace_id})

        async def on_close():
            try:
                await scheduler.cancel(user_id, job, "client_disconnected")
            except (ApiError, sqlite3.Error):
                scheduler.fatal, scheduler.state = True, "paused"

        return InferenceStream(body_events(), on_close, settings.stream_send_timeout_seconds,
                               headers={"Location":"/api/v1/jobs/"+job, "X-Accel-Buffering":"no"})

    @app.get("/api/v1/jobs/by-request/{request_id}")
    async def by_request(request_id: str,user_id=Depends(user)):
        return await state(await scheduler.db(lambda:store.get(user_id,request_id=uuid(request_id))))

    @app.get("/api/v1/jobs")
    async def listing(limit: int=20,beforeSequence: int | None=None,user_id=Depends(user)):
        if not 1<=limit<=50 or (beforeSequence is not None and beforeSequence<1):
            raise ApiError(400,"INVALID_REQUEST")
        rows,more = await scheduler.db(store.list,user_id,limit,beforeSequence)
        items = [{"sequence":r["sequence"],"jobId":r["job_id"],"clientRequestId":r["client_request_id"],
                  "status":r["status"],"createdAt":iso(r["created_at_ms"]),"finishedAt":iso(r["finished_at_ms"]),
                  "errorCode":r["error_code"],"resultAvailable":available(r)} for r in rows]
        return {"items":items,"nextBeforeSequence":rows[-1]["sequence"] if more else None}

    @app.get("/api/v1/jobs/{job_id}")
    async def get_status(job_id: str,user_id=Depends(user)):
        return await state(await scheduler.db(lambda:store.get(user_id,job=uuid(job_id))))

    @app.get("/api/v1/jobs/{job_id}/result")
    async def result(job_id: str,user_id=Depends(user)):
        row = await scheduler.db(lambda:store.get(user_id,job=uuid(job_id)))
        if row["status"] not in TERMINAL:
            raise ApiError(409,"JOB_NOT_FINISHED")
        if not available(row) or row["request_json"] is None:
            raise ApiError(410,"RESULT_EXPIRED")
        return result_response(row)

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel(job_id: str,user_id=Depends(user)):
        job_id = uuid(job_id)
        status,accepted,running,confirmed = await scheduler.cancel(user_id,job_id)
        return JSONResponse({"jobId":job_id,"status":status,"cancelAccepted":accepted,
                             "inferenceStopConfirmed":confirmed,"schedulerState":scheduler.state},
                            status_code=202 if running else 200)
    register_openai(app, settings, scheduler, store, user)
    return app

import asyncio
from contextlib import asynccontextmanager
import sqlite3
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from aiserver.api.errors import ApiError, safe_error
from aiserver.api.presentation import available, iso, result_response, status_response
from aiserver.api.validation import RequestValidator
from aiserver.inference.fake import FakeBudget, FakeProvider, fake_profile
from aiserver.jobs.scheduler import Scheduler
from aiserver.operations.lock import InstanceLock
from aiserver.settings import Settings
from aiserver.storage.db import Store, TERMINAL, now_ms

def create_app(settings=None, *, profile=None, budget=None, provider=None):
    settings = settings or Settings.load()
    store = Store(settings)
    profile = profile or fake_profile(settings)
    budget = budget or FakeBudget(settings.fake_max_material_bytes)
    provider = provider or FakeProvider(settings.fake_delay_seconds,settings.fake_scenario,settings.fake_late_seconds)
    scheduler = Scheduler(store,settings,profile,budget,provider)
    validator = RequestValidator(settings.contract_directory)

    @asynccontextmanager
    async def lifespan(app):
        lock = InstanceLock(settings.data_directory).acquire()
        try:
            await asyncio.to_thread(store.initialize)
            await scheduler.start()
            try:
                yield
            finally:
                await scheduler.close()
        finally:
            lock.release()

    app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=settings.allowed_hosts)
    app.state.store, app.state.scheduler, app.state.settings = store, scheduler, settings

    @app.middleware("http")
    async def headers(request, call_next):
        request.state.trace_id = str(uuid4())
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    def error_response(request,error):
        headers = {"Retry-After":str(settings.poll_seconds)} if error.status in (429,503) else {}
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
            raise ApiError(401,"UNAUTHORIZED")
        return await asyncio.to_thread(store.authenticate,value[7:])

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

    @app.get("/api/v1/health")
    async def health():
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
                             "storage":"ok" if not scheduler.fatal else "unavailable"},
                            status_code=200 if can_accept else 503)

    @app.post("/api/v1/jobs")
    async def submit(request: Request, user_id=Depends(user)):
        if request.headers.get("content-type","").split(";")[0].lower() != "application/json":
            raise ApiError(415,"UNSUPPORTED_MEDIA_TYPE")
        if request.headers.get("content-encoding","identity").lower() != "identity":
            raise ApiError(415,"UNSUPPORTED_MEDIA_TYPE")
        length = request.headers.get("content-length")
        if length:
            try:
                size = int(length)
            except ValueError:
                raise ApiError(400,"INVALID_REQUEST") from None
            if size < 0:
                raise ApiError(400,"INVALID_REQUEST")
            if size > settings.max_request_bytes:
                raise ApiError(413,"REQUEST_TOO_LARGE")
        body = bytearray()
        try:
            async with asyncio.timeout(settings.upload_timeout_seconds):
                async for chunk in request.stream():
                    if len(body)+len(chunk)>settings.max_request_bytes:
                        raise ApiError(413,"REQUEST_TOO_LARGE")
                    body.extend(chunk)
        except TimeoutError:
            raise ApiError(408,"UPLOAD_TIMEOUT") from None
        normalized,fingerprint,task = await asyncio.to_thread(validator.validate,bytes(body))
        row,replayed = await scheduler.submit(user_id,normalized,fingerprint,task)
        return JSONResponse({"jobId":row["job_id"],"clientRequestId":row["client_request_id"],
                             "status":row["status"],"replayed":replayed,"pollAfterSeconds":settings.poll_seconds},
                            status_code=200 if replayed else 202,
                            headers={"Location":"/api/v1/jobs/"+row["job_id"]})

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
    return app

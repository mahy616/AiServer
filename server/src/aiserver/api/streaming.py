"""SSE with bounded network writes and a single owner for each generation."""
import asyncio
import json

from starlette.responses import StreamingResponse


def event(kind, data):
    return ("event: " + kind + "\ndata: " + json.dumps(data, ensure_ascii=False,
            separators=(",", ":")) + "\n\n").encode("utf-8")


class InferenceStream(StreamingResponse):
    def __init__(self, body, on_close, timeout, **kwargs):
        super().__init__(body, media_type="text/event-stream", **kwargs)
        self.on_close, self.send_timeout = on_close, timeout

    async def write(self, scope, receive, send):
        await asyncio.wait_for(send({"type":"http.response.start", "status":self.status_code,
                                    "headers":self.raw_headers}), self.send_timeout)
        async for chunk in self.body_iterator:
            await asyncio.wait_for(send({"type":"http.response.body", "body":chunk,
                                        "more_body":True}), self.send_timeout)
        await asyncio.wait_for(send({"type":"http.response.body", "body":b"", "more_body":False}), self.send_timeout)

    async def __call__(self, scope, receive, send):
        async def disconnected():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    return

        tasks = [asyncio.create_task(self.write(scope, receive, send)), asyncio.create_task(disconnected())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except (OSError, TimeoutError):
                    pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # ASGI disconnect/task cancellation must not cancel the durable stop write.
            cleanup = asyncio.create_task(self.on_close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            await self.body_iterator.aclose()


class InferenceJSON(InferenceStream):
    """Wait without sending headers; disconnect still cancels the durable job."""
    def __init__(self, complete, on_close, timeout, **kwargs):
        async def empty():
            if False:
                yield b""
        super().__init__(empty(), on_close, timeout, **kwargs)
        self.complete = complete

    async def write(self, scope, receive, send):
        response = await self.complete()
        response.raw_headers.extend((key, value) for key, value in self.raw_headers
                                    if key not in (b"content-type", b"content-length"))
        async def bounded(message):
            await asyncio.wait_for(send(message), self.send_timeout)
        await response(scope, receive, bounded)


class TraceHeadersMiddleware:
    """Pure ASGI: avoid a buffering middleware around the streaming response."""
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from uuid import uuid4
        from aiserver.storage.db import now_ms
        trace = str(uuid4())
        scope.setdefault("state", {})["trace_id"] = trace
        scope["state"]["received_at_ms"] = now_ms()
        async def traced(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"), (b"x-trace-id", trace.encode("ascii"))]
                if hasattr(self.state, "logger"):
                    self.state.logger.info("request trace=%s method=%s status=%s", trace,
                                           scope["method"], message["status"])
            await send(message)
        from aiserver.inference.diagnostics import inference_context
        with inference_context(trace_id=trace):
            await self.app(scope, receive, traced)

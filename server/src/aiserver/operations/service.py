"""One console/event loop owns API, model and the optional background tunnel."""
import asyncio
import time

import httpx

from aiserver.operations.tunnel import ManagedTunnel
from aiserver.operations.diagnostics import StartupFailure


async def supervise(server, app, settings, tunnel, report, failed):
    stopping = asyncio.Event()
    watcher = None
    try:
        deadline = time.monotonic() + settings.service_startup_timeout_seconds
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            while not server.should_exit:
                if server.started:
                    try:
                        ready, _ = await app.state.scheduler.can_accept()
                        if ready:
                            response = await client.get(f"http://127.0.0.1:{settings.port}/api/v1/health", timeout=2)
                            if response.status_code == 200:
                                break
                    except httpx.HTTPError:
                        pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("SERVICE_STARTUP_TIMEOUT")
                await asyncio.sleep(0.1)
            if server.should_exit:
                return
        report(f"[API/model] ready at http://127.0.0.1:{settings.port}; provider={settings.provider}.")
        if hasattr(app.state,"monitor"):
            app.state.monitor.local_ready = True
            app.state.monitor.tunnel = tunnel
        if tunnel:
            await tunnel.start()
            watcher = asyncio.create_task(tunnel.watch(stopping))
        else:
            report("[NATAPP] disabled; public endpoint health is checked separately when configured.")
        while not server.should_exit:
            if watcher and watcher.done():
                watcher.result()
                raise RuntimeError("NATAPP_SUPERVISOR_STOPPED")
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Values from INI files, child output and public request bodies never go here.
        code = str(error)
        failed.append(code if code.isascii() and code.replace("_", "").isalnum() and code.isupper()
                      else "SERVICE_SUPERVISOR_FAILED")
        server.should_exit = True
    finally:
        if hasattr(app.state, "scheduler"):
            app.state.scheduler.stopping = True
            app.state.scheduler.wake.set()
        stopping.set()
        if watcher:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if tunnel:
            await tunnel.close()


async def serve(settings, *, app=None, server=None, tunnel_factory=ManagedTunnel, report=print):
    import uvicorn
    from aiserver.app import create_app
    report("[startup] checking configuration; starting one API/model owner.")
    tunnel = tunnel_factory(settings, report) if settings.manage_natapp else None
    # Missing private configuration must fail before model startup, not silently run a second tunnel.
    if tunnel:
        tunnel.validate()
    app = app or create_app(settings)
    if hasattr(app.state,"monitor"):
        app.state.monitor.report = report
    server = server or uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.port, workers=1,
                  access_log=False, proxy_headers=False, timeout_graceful_shutdown=45, ws="none"))
    failed = []
    monitor = asyncio.create_task(supervise(server, app, settings, tunnel, report, failed))
    try:
        await server.serve()
    finally:
        server.should_exit = True
        try:
            await monitor
        finally:
            if tunnel:
                await tunnel.close()
    if failed:
        raise RuntimeError(failed[0])
    if not server.started:
        problem = getattr(app.state,"startup_failure",None)
        if problem:
            raise StartupFailure(problem)
        raise RuntimeError("API_STARTUP_FAILED")
    report("[shutdown] owned API/model/NATAPP stopped.")

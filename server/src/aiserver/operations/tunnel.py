"""Supervise the configured NATAPP child, without exposing its INI or output."""
import asyncio
import configparser
import json
import os
from pathlib import Path
import subprocess
import time

from aiserver.operations.engine import WindowsJob, process_identity
from aiserver.operations.lock import InstanceLock
from aiserver.operations.processes import matching_processes, resume_suspended
from aiserver.settings import ROOT


class ManagedTunnel:
    def __init__(self, settings, report=print):
        self.settings, self.report = settings, report
        self.executable = settings.natapp_executable.resolve()
        self.config = settings.natapp_config.resolve()
        self.directory = settings.data_directory / "managed-natapp"
        self.record = self.directory / "process.json"
        self.child = self.job = self.lock = None
        self.state = "stopped"

    def validate(self):
        if os.name != "nt":
            raise RuntimeError("NATAPP_WINDOWS_REQUIRED")
        if not self.executable.is_file():
            raise RuntimeError("NATAPP_EXECUTABLE_MISSING")
        if not self.config.is_file():
            raise RuntimeError("NATAPP_CONFIG_MISSING")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(self.config.read_text(encoding="utf-8-sig"))
            token = parser.get("default", "authtoken", fallback="").strip()
            log = parser.get("default", "log", fallback="none").strip().casefold()
        except (OSError, UnicodeError, configparser.Error):
            raise RuntimeError("NATAPP_CONFIG_INVALID") from None
        if not token or token == "REPLACE_LOCALLY":
            raise RuntimeError("NATAPP_TOKEN_NOT_CONFIGURED")
        if log != "none":
            raise RuntimeError("NATAPP_LOG_MUST_BE_NONE")

    def _stop_owned(self):
        try:
            if self.job is not None:
                self.job.terminate()
                self.job.close()
                self.job = None
                self.child.wait(timeout=10)
            elif self.record.exists():
                try:
                    saved = json.loads(self.record.read_text(encoding="utf-8"))
                    observed = process_identity(saved["pid"])
                except (ProcessLookupError, FileNotFoundError):
                    self.record.unlink(missing_ok=True)
                    return
                except (ValueError, KeyError, TypeError, OSError):
                    raise RuntimeError("NATAPP_RECORD_INVALID") from None
                if observed != saved["identity"] or Path(observed["executable"]) != self.executable:
                    # A reused PID belongs to somebody else. Never kill it.
                    self.record.unlink(missing_ok=True)
                    return
                result = subprocess.run(["taskkill", "/PID", str(saved["pid"]), "/T", "/F"],
                                        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                if result.returncode:
                    raise RuntimeError("NATAPP_STOP_FAILED")
            self.record.unlink(missing_ok=True)
        finally:
            self.state = "stopped"

    def _launch(self):
        self.validate()
        if self.lock is None:
            self.lock = InstanceLock(self.directory).acquire()
        self._stop_owned()
        if matching_processes({self.executable.name, "natapp.exe"}):
            raise RuntimeError("NATAPP_UNMANAGED_INSTANCE")
        environment = os.environ.copy()
        environment.pop("AISERVER_TOKEN", None)
        self.child = subprocess.Popen([str(self.executable), "-config=" + str(self.config)], cwd=ROOT, env=environment,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004)  # CREATE_SUSPENDED
        try:
            self.job = WindowsJob(self.child)
            self.record.write_text(json.dumps({"pid":self.child.pid,
                                   "identity":process_identity(self.child.pid)}), encoding="utf-8")
            resume_suspended(self.child)
        except BaseException:
            if self.job:
                self.job.close(); self.job = None
            elif self.child.poll() is None:
                self.child.terminate()
            self.child.wait(timeout=10)
            self.record.unlink(missing_ok=True)
            raise
        self.state = "running"
        self.report("[NATAPP] process running; public endpoint health is checked separately.")

    async def start(self):
        launch = asyncio.create_task(asyncio.to_thread(self._launch))
        try:
            await asyncio.shield(launch)
        except asyncio.CancelledError:
            try:
                await launch
            finally:
                await self.close()
            raise

    async def watch(self, stopping):
        delay = self.settings.natapp_restart_initial_seconds
        since = time.monotonic()
        while not stopping.is_set():
            try:
                await asyncio.wait_for(stopping.wait(), 0.25)
                return
            except TimeoutError:
                pass
            exit_code = self.child.poll()
            if exit_code is None:
                continue
            self.state = "retrying"
            if time.monotonic() - since >= 60:
                delay = self.settings.natapp_restart_initial_seconds
            self.report(f"[NATAPP] process exited (code {exit_code}); public connectivity: unavailable. Retry in {delay:g}s; API/model stay local.")
            try:
                await asyncio.wait_for(stopping.wait(), delay)
                return
            except TimeoutError:
                pass
            try:
                await self.start()
                since = time.monotonic()
            except (RuntimeError, OSError, subprocess.SubprocessError):
                # Do not leak executable/INI output or alter an unmanaged process.
                self.report("[NATAPP] restart failed; inspect local NATAPP configuration/processes.")
            delay = min(delay * 2, self.settings.natapp_restart_max_seconds)

    def _close(self):
        # A failed/duplicate API never acquired this tunnel's ownership lock.
        if self.lock is None and self.job is None:
            return
        try:
            self._stop_owned()
        finally:
            if self.lock:
                self.lock.release()
                self.lock = None

    async def close(self):
        await asyncio.to_thread(self._close)

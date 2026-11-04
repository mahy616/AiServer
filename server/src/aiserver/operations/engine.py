"""Own only the engine launched by this project; verify PID identity before recovery."""
import asyncio
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import subprocess
import threading
from urllib.parse import urlsplit

from aiserver.operations.lock import InstanceLock
ROOT = Path(__file__).resolve().parents[4]


class WindowsJob:
    """Kernel-owned process tree. Closing the API also closes its entire engine tree."""
    def __init__(self, process):
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                        ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise RuntimeError("Cannot establish managed engine job")
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise RuntimeError("Cannot attach engine to managed job")

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise RuntimeError("Cannot terminate managed engine job")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def process_identity(pid):
    if os.name != "nt":
        return {"executable": os.readlink(f"/proc/{pid}/exe"),
                "created": Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]}
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ProcessLookupError(pid)
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise OSError("Cannot inspect engine process state")
        if exit_code.value != 259:  # STILL_ACTIVE; a terminated Windows process may retain its PID/handle.
            raise ProcessLookupError(pid)
        name, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        created, exited, system, user = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) or not kernel.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            raise OSError("Cannot verify engine identity")
        return {"executable": str(Path(name.value).resolve()),
                "created": (created.dwHighDateTime << 32) | created.dwLowDateTime}
    finally:
        kernel.CloseHandle(handle)


class ManagedEngine:
    def __init__(self, cfg, log_directory):
        self.cfg = cfg
        self.executable = (ROOT / cfg["ExecutablePath"]).resolve()
        self.lock_directory = ROOT / ".local" / "managed-engine"
        self.record = self.lock_directory / "process.json"
        self.lock = None
        self.child = None
        self.job = None
        self.reader = None
        self.log_directory = log_directory

    def _stop_recorded(self):
        if self.job:
            self.job.terminate()
            self.job.close()
            self.job = None
            self.child.wait(timeout=15)
            self.record.unlink(missing_ok=True)
            return
        if not self.record.exists():
            return
        record = json.loads(self.record.read_text(encoding="utf-8"))
        try:
            identity = process_identity(record["pid"])
        except (ProcessLookupError, FileNotFoundError):
            self.record.unlink(missing_ok=True)
            return
        if identity != record["identity"] or Path(identity["executable"]) != self.executable:
            # Stale PID has been reused. Leave the unrelated process untouched.
            self.record.unlink(missing_ok=True)
            return
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(record["pid"]), "/T", "/F"],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            if result.returncode:
                raise RuntimeError("Managed engine could not be stopped")
        else:
            import signal
            os.killpg(record["pid"], signal.SIGKILL)
        if self.child:
            self.child.wait(timeout=15)
        self.record.unlink(missing_ok=True)

    def _launch(self):
        if self.lock is None:
            self.lock = InstanceLock(self.lock_directory).acquire()
        self._stop_recorded()
        uri = urlsplit(self.cfg["BaseUrl"])
        with socket.socket() as probe:
            if probe.connect_ex((uri.hostname, uri.port)) == 0:
                from aiserver.operations.processes import engine_port_problem
                from aiserver.operations.diagnostics import StartupFailure
                raise StartupFailure(engine_port_problem(uri.port,self.record))
        if not self.executable.is_file():
            raise RuntimeError("Engine executable missing")
        command, environment, cwd = self.launch_spec()
        # The manager's stdin is its private stop pipe. A Python engine must not
        # inherit that pipe while the manager's listener is reading it.
        self.child = subprocess.Popen(command, env=environment, cwd=cwd,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
                                      start_new_session=os.name != "nt")
        if os.name == "nt":
            try:
                self.job = WindowsJob(self.child)
                from aiserver.operations.processes import resume_suspended
                self.record.write_text(json.dumps({"pid": self.child.pid, "identity": process_identity(self.child.pid)}), encoding="utf-8")
                resume_suspended(self.child)
            except BaseException:
                if self.job:
                    self.job.close(); self.job = None
                elif self.child.poll() is None:
                    self.child.terminate()
                self.child.wait(timeout=15)
                self.record.unlink(missing_ok=True)
                raise
        else:
            self.record.write_text(json.dumps({"pid": self.child.pid, "identity": process_identity(self.child.pid)}), encoding="utf-8")
        self.log_directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(self.log_directory / "engine.log", maxBytes=5242880, backupCount=5, encoding="utf-8")
        pipe = self.child.stdout
        def drain():
            try:
                for raw in iter(pipe.readline, b""):
                    handler.emit(logging.LogRecord("engine", logging.INFO, "", 0, raw.decode("utf-8", "replace").rstrip(), (), None))
            finally:
                pipe.close()
                handler.close()
        self.reader = threading.Thread(target=drain, daemon=True)
        self.reader.start()

    def launch_spec(self):
        from aiserver.inference.configuration import engine_environment
        return [str(self.executable), "serve"], engine_environment(self.cfg, ROOT), ROOT

    async def start(self):
        await asyncio.to_thread(self._launch)
        import httpx
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(60):
                if self.child.poll() is not None:
                    raise RuntimeError("Managed engine exited during startup")
                try:
                    response = await client.get(self.cfg["BaseUrl"] + "/api/version", timeout=1)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        raise RuntimeError("Managed engine startup timed out")

    async def restart(self):
        await asyncio.to_thread(self._stop_recorded)
        if self.reader:
            await asyncio.to_thread(self.reader.join, 2)
        await self.start()

    async def close(self):
        try:
            if self.lock:
                await asyncio.to_thread(self._stop_recorded)
            if self.reader:
                await asyncio.to_thread(self.reader.join, 2)
        finally:
            if self.lock:
                self.lock.release()
                self.lock = None

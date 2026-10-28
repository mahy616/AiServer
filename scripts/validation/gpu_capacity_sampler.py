"""NVIDIA dedicated VRAM and Windows per-process GPU shared-memory sampling."""
import ctypes
import os
import re
import threading
import time

from aiserver.operations.monitoring import HostSampler
from aiserver.operations.processes import matching_processes


def engine_process_names(executable):
    return tuple(dict.fromkeys((executable, "llama-server.exe")))


class SharedGpuMemory:
    def __init__(self, executable):
        if os.name != "nt":
            raise OSError("Windows GPU counters required")
        from ctypes import wintypes as w
        self.w, self.executable = w, executable
        self.backend_pids, self.observed_pids = set(), set()
        self.api = ctypes.WinDLL("pdh")
        self.query = w.HANDLE()
        self.counter = w.HANDLE()
        signatures = {
            "PdhOpenQueryW": [w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(w.HANDLE)],
            "PdhAddEnglishCounterW": [w.HANDLE, w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(w.HANDLE)],
            "PdhAddCounterW": [w.HANDLE, w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(w.HANDLE)],
            "PdhGetCounterInfoW": [w.HANDLE, w.BOOL, ctypes.POINTER(w.DWORD), ctypes.c_void_p],
            "PdhExpandWildCardPathW": [w.LPCWSTR, w.LPCWSTR, w.LPWSTR, ctypes.POINTER(w.DWORD), w.DWORD],
            "PdhCollectQueryData": [w.HANDLE],
            "PdhGetFormattedCounterValue": [w.HANDLE, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p],
            "PdhCloseQuery": [w.HANDLE],
        }
        for name, args in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, w.DWORD
        self.handles = {}
        try:
            self.check(self.api.PdhOpenQueryW(None, 0, ctypes.byref(self.query)))
            self.check(self.api.PdhAddEnglishCounterW(self.query,
                r"\GPU Process Memory(*)\Shared Usage", 0, ctypes.byref(self.counter)))
            # Localize the object/counter names, then expand instances as required
            # by PdhAddEnglishCounterW's documented wildcard procedure.
            size = w.DWORD()
            self.api.PdhGetCounterInfoW(self.counter, False, ctypes.byref(size), None)
            if not size.value:
                raise OSError("GPU counter metadata unavailable")
            buffer = ctypes.create_string_buffer(size.value)
            self.check(self.api.PdhGetCounterInfoW(self.counter, False, ctypes.byref(size), buffer))
            class Info(ctypes.Structure):
                _fields_ = [(k, w.DWORD) for k in ("length", "type", "version", "status", "scale", "default_scale")] + [
                    ("user", ctypes.c_size_t), ("query_user", ctypes.c_size_t), ("full_path", w.LPWSTR)]
            self.path = ctypes.cast(buffer, ctypes.POINTER(Info)).contents.full_path
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(status):
        if status:
            raise OSError("GPU shared-memory counter unavailable")

    def sample(self):
        w = self.w
        self.backend_pids = set(matching_processes(["llama-server.exe"]))
        self.observed_pids = set()
        pids = set(matching_processes([self.executable])) | self.backend_pids
        # The pinned engine runs CUDA in llama-server.exe. Frontend-only
        # counters cannot establish the runner's shared-memory footprint.
        if not self.backend_pids:
            return None
        size = w.DWORD()
        self.api.PdhExpandWildCardPathW(None, self.path, None, ctypes.byref(size), 0)
        if not size.value:
            raise OSError("GPU shared-memory instances unavailable")
        buffer = ctypes.create_unicode_buffer(size.value)
        self.check(self.api.PdhExpandWildCardPathW(None, self.path, buffer, ctypes.byref(size), 0))
        active = []
        for path in buffer[:size.value].split("\0"):
            match = re.search(r"\(pid_(\d+)_", path, re.I)
            if not match or int(match[1]) not in pids:
                continue
            if path not in self.handles:
                handle = w.HANDLE()
                self.check(self.api.PdhAddCounterW(self.query, path, 0, ctypes.byref(handle)))
                self.handles[path] = handle
            active.append(self.handles[path])
            self.observed_pids.add(int(match[1]))
        if not active or not self.backend_pids.issubset(self.observed_pids):
            raise OSError("GPU process memory not observed")
        self.check(self.api.PdhCollectQueryData(self.query))
        class Number(ctypes.Union):
            _fields_ = [("large", ctypes.c_int64), ("double", ctypes.c_double)]
        class Value(ctypes.Structure):
            _fields_ = [("status", w.DWORD), ("number", Number)]
        total = 0
        for handle in active:
            value = Value()
            self.check(self.api.PdhGetFormattedCounterValue(handle, 0x400, None, ctypes.byref(value)))
            if value.status not in (0, 1) or value.number.large < 0:
                raise OSError("Invalid GPU shared-memory sample")
            total += value.number.large
        return total

    def close(self):
        if self.query:
            self.api.PdhCloseQuery(self.query)
            self.query = None


class GpuSamples:
    def __init__(self, executable, output):
        self.host = HostSampler()
        self.shared = SharedGpuMemory(executable)
        self.output = output
        self.stop = threading.Event()
        self.thread = None

    def sample(self):
        result = {"at": time.time(), **self.host.sample(), "shared_bytes": None}
        try:
            result["shared_bytes"] = self.shared.sample()
        except OSError:
            result["shared_error"] = "GPU_SHARED_COUNTER_UNAVAILABLE"
        result["shared_backend_pids"] = sorted(self.shared.backend_pids)
        result["shared_observed_pids"] = sorted(self.shared.observed_pids)
        result["shared_backend_verified"] = bool(self.shared.backend_pids and
            self.shared.backend_pids.issubset(self.shared.observed_pids) and result["shared_bytes"] is not None)
        self.output.append(result)

    def __enter__(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.sample()
                except Exception:
                    self.output.append({"at": time.time(), "gpu_error": "SAMPLING_FAILED"})
                self.stop.wait(1)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        try:
            self.sample()
        finally:
            self.shared.close()


def validate_samples(samples, max_shared_bytes):
    from context_capacity import CapacityFailure
    shared = [s["shared_bytes"] for s in samples if s.get("shared_bytes") is not None and s.get("shared_backend_verified")]
    gpu = [g for s in samples for g in s.get("gpus", []) if g.get("memory_used_mib") is not None]
    if (not shared or not samples[-1].get("shared_backend_verified") or not gpu or any(type(g.get("memory_total_mib")) not in (int, float)
            or g["memory_total_mib"] <= 0 for g in gpu)):
        raise RuntimeError("GPU_MEMORY_COUNTERS_UNAVAILABLE")
    if max(shared) > max_shared_bytes:
        raise CapacityFailure("GPU_SHARED_MEMORY_USED")
    return {"shared_peak_bytes": max(shared), "gpus": gpu,
            "peak_used_fraction": max(g["memory_used_mib"] / g["memory_total_mib"] for g in gpu)}

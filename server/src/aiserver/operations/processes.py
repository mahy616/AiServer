"""Small Windows process inventory/resume helpers; no process-name termination."""
import ctypes
import os
from pathlib import Path
import json
import socket


def _kernel():
    if os.name != "nt":
        raise RuntimeError("NATAPP_WINDOWS_REQUIRED")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


def matching_processes(names):
    from ctypes import wintypes
    class Process(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
    kernel = _kernel()
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Process)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Process)]
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise RuntimeError("PROCESS_INVENTORY_FAILED")
    try:
        entry = Process(); entry.size = ctypes.sizeof(entry)
        found = []
        wanted = {str(name).casefold() for name in names}
        valid = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while valid:
            if Path(entry.name).name.casefold() in wanted:
                from aiserver.operations.engine import process_identity
                try:
                    process_identity(entry.pid)
                except (ProcessLookupError, FileNotFoundError):
                    pass
                except OSError:
                    found.append(entry.pid)  # Cannot verify; refuse competing startup.
                else:
                    found.append(entry.pid)
            valid = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, 18):  # ERROR_NO_MORE_FILES
            raise RuntimeError("PROCESS_INVENTORY_FAILED")
        return found
    finally:
        kernel.CloseHandle(snapshot)


def port_owners(port, record=None):
    """IPv4 listener identities only; never inspect arbitrary command lines."""
    if os.name != "nt": return []
    from ctypes import wintypes
    from aiserver.operations.engine import process_identity
    saved = None
    if record:
        try: saved = json.loads(Path(record).read_text(encoding="utf-8"))
        except (OSError,ValueError): pass
    class Row(ctypes.Structure):
        _fields_ = [(key,wintypes.DWORD) for key in ("state","local_address","local_port","remote_address","remote_port","pid")]
    api = ctypes.WinDLL("iphlpapi",use_last_error=True)
    api.GetExtendedTcpTable.argtypes = [ctypes.c_void_p,ctypes.POINTER(wintypes.DWORD),wintypes.BOOL,wintypes.ULONG,ctypes.c_int,wintypes.ULONG]
    size = wintypes.DWORD()
    api.GetExtendedTcpTable(None,ctypes.byref(size),False,2,3,0)
    if not size.value: return []
    buffer = ctypes.create_string_buffer(size.value)
    if api.GetExtendedTcpTable(buffer,ctypes.byref(size),False,2,3,0): return []
    count = wintypes.DWORD.from_buffer(buffer).value
    found = []
    for index in range(count):
        row = Row.from_buffer(buffer,4+index*ctypes.sizeof(Row))
        if socket.ntohs(row.local_port & 0xffff) != port: continue
        item = {"pid":row.pid,"executable":None,"owned":False}
        try:
            identity = process_identity(row.pid)
            item["executable"] = identity["executable"]
            item["owned"] = bool(isinstance(saved,dict) and saved.get("pid")==row.pid and saved.get("identity")==identity)
        except OSError: pass
        found.append(item)
    return found


def engine_port_problem(port, record=None):
    from aiserver.operations.diagnostics import PROBLEMS, STAGES
    try: owners = port_owners(port,record)
    except (OSError,ValueError): owners = []
    details = "；".join(f'PID {item["pid"]}，程序 {item["executable"] or "路径暂不可读"}，'+
        ("本工程记录匹配" if item["owned"] else "未确认属于本工程") for item in owners)
    message = f"Ollama监听端口 {port} 已占用。"+(details+"。" if details else "暂无法读取监听PID。")
    return {"code":"ENGINE_PORT_IN_USE","phase":"engine","stage":STAGES["engine"],
            "message":message+PROBLEMS["ENGINE_PORT_IN_USE"],"errorType":"EnginePortConflict","owners":owners}


def resume_suspended(process):
    """Resume only threads of our CREATE_SUSPENDED child after Job assignment."""
    from ctypes import wintypes
    class Thread(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("tid", wintypes.DWORD), ("owner", wintypes.DWORD),
                    ("priority", wintypes.LONG), ("delta", wintypes.LONG), ("flags", wintypes.DWORD)]
    kernel = _kernel()
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(Thread)]
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(Thread)]
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise RuntimeError("CHILD_RESUME_FAILED")
    resumed = 0
    try:
        entry = Thread(); entry.size = ctypes.sizeof(entry)
        valid = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while valid:
            if entry.owner == process.pid:
                handle = kernel.OpenThread(0x2, False, entry.tid)
                if not handle:
                    raise RuntimeError("CHILD_RESUME_FAILED")
                try:
                    if kernel.ResumeThread(handle) == 0xFFFFFFFF:
                        raise RuntimeError("CHILD_RESUME_FAILED")
                    resumed += 1
                finally:
                    kernel.CloseHandle(handle)
            valid = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        if not resumed:
            raise RuntimeError("CHILD_RESUME_FAILED")
    finally:
        kernel.CloseHandle(snapshot)

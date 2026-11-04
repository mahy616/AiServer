import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/"scripts/server"))
import manager_support as support
import ManagerActions as actions
from aiserver.operations.lock import InstanceLock


def configuration(tmp_path):
    data = tmp_path/"data"
    path = tmp_path/"server.local.json"
    value = {"environment":"Development","provider":"Fake","manage_natapp":False,
             "data_directory":str(data),"port":18080,"inference_config":"keep-model-profile.json",
             "allowed_hosts":["127.0.0.1","example.test"],"fake_delay_seconds":0}
    path.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    return path,value


def test_configuration_merge_preserves_unknown_fields_backups_and_conflicts(tmp_path):
    path,value=configuration(tmp_path)
    value["future_setting"]={"privateValue":"SYNTHETIC_SECRET_DO_NOT_DISPLAY"}
    path.write_text(json.dumps(value),encoding="utf-8")
    before=path.read_bytes()
    inspected=support.inspect_config(ROOT,path)
    assert "SYNTHETIC_SECRET" not in json.dumps(inspected)
    fields=inspected["fields"]
    fields.update({"payload_days":1,"max_request_bytes":20971520})
    saved=support.save_config(ROOT,path,fields,inspected["sha256"])
    assert Path(saved["backup"]).read_bytes()==before
    after=json.loads(path.read_bytes())
    for key in value:
        if key not in support.EDITABLE: assert value[key]==after[key]
    with pytest.raises(ValueError,match="修改"):
        support.save_config(ROOT,path,fields,inspected["sha256"])
    assert len(list(tmp_path.glob("*.bak")))==1


def test_config_save_offline_lock_and_missing_tunnel_validation(tmp_path):
    path,value=configuration(tmp_path)
    original=path.read_bytes()
    inspected=support.inspect_config(ROOT,path)
    with support.offline(ROOT,value):
        with pytest.raises(RuntimeError,match="active instance"):
            support.save_config(ROOT,path,inspected["fields"],inspected["sha256"])
    assert path.read_bytes()==original
    fields={**inspected["fields"],"manage_natapp":True}
    fields.update({"natapp_executable":str(tmp_path/"missing.exe"),"natapp_config":str(tmp_path/"missing.ini")})
    with pytest.raises(ValueError,match="不存在"):
        support.save_config(ROOT,path,fields,inspected["sha256"])
    assert path.read_bytes()==original


def test_legacy_backup_no_migration_missing_db_and_lock(tmp_path,monkeypatch):
    monkeypatch.setattr(support,"load_lock",lambda root:InstanceLock)
    path,value=configuration(tmp_path)
    with pytest.raises(ValueError,match="尚无"):
        support.backup_database(tmp_path,path)
    data=Path(value["data_directory"]); data.mkdir()
    database=data/"jobs.db"
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE evidence(value TEXT)")
        conn.execute("INSERT INTO evidence VALUES('keep')")
        conn.execute("PRAGMA user_version=1"); conn.commit()
        result=support.backup_database(tmp_path,path)
    assert result["schemaVersion"]==1
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("SELECT value FROM evidence").fetchone()[0]=="keep"
        assert backup.execute("PRAGMA user_version").fetchone()[0]==1
    with sqlite3.connect(database) as current:
        assert current.execute("PRAGMA user_version").fetchone()[0]==1
    with support.offline(ROOT,value):
        with pytest.raises(RuntimeError): support.backup_database(tmp_path,path)


def test_private_stdio_accounts_and_key_management_without_secret_listing(tmp_path,monkeypatch):
    path,value=configuration(tmp_path)
    monkeypatch.setenv("AISERVER_TOKEN","SYNTHETIC_OLD_TOKEN")
    monkeypatch.setenv("AISERVER_DATA_DIRECTORY",str(tmp_path/"wrong"))
    environment=support.child_environment(ROOT)
    assert not any(key.startswith("AISERVER_") for key in environment)
    args=[sys.executable,str(ROOT/"scripts/server/ManagerActions.py"),"create-user"]
    result=subprocess.run(args,input=json.dumps({"config":str(path),"username":"gui-user","displayName":"GUI User"})+"\n",text=True,encoding="utf-8",capture_output=True,
        env=environment,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)["result"]["users"][0]["user_id"]=="gui-user"
    assert "apiKey" not in result.stdout and not (tmp_path/"wrong").exists()
    monkeypatch.delenv("AISERVER_DATA_DIRECTORY")
    values={"config":str(path),"username":"gui-user"}
    key=actions.perform("token-create",values)["apiKey"]
    listing=actions.perform("token-list",values)
    assert key not in json.dumps(listing) and "token_sha256" not in json.dumps(listing)
    key_id=key.split("_")[1]
    assert listing["keys"][0]["key_id"]==key_id
    revoked=actions.perform("token-revoke",{**values,"keyId":key_id})
    assert revoked["keys"][0]["status"]=="revoked"
    actions.perform("create-user",{**values,"username":"other-user"})
    with pytest.raises(ValueError,match="TOKEN_NOT_FOUND"):
        actions.perform("token-revoke",{**values,"username":"other-user","keyId":key_id})


def test_manager_host_real_loopback_stop_via_private_pipe(tmp_path):
    path,value=configuration(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); value["port"]=sock.getsockname()[1]
    path.write_text(json.dumps(value),encoding="utf-8")
    process=subprocess.Popen([sys.executable,"-u",str(ROOT/"scripts/server/ManagerActions.py"),"serve"],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",
        env=support.child_environment(ROOT),creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    try:
        process.stdin.write(json.dumps({"config":str(path)})+"\n"); process.stdin.flush()
        until=time.monotonic()+8
        with httpx.Client(trust_env=False) as client:
            while time.monotonic()<until:
                try:
                    if client.get(f'http://127.0.0.1:{value["port"]}/api/v1/health',timeout=0.3).status_code==200: break
                except httpx.HTTPError: pass
                time.sleep(0.05)
            else: pytest.fail("Manager host did not become ready")
        until=time.monotonic()+8
        while time.monotonic()<until:
            observed=actions.perform("monitor",{"config":str(path)})
            if observed["fresh"]: break
            time.sleep(.05)
        else: pytest.fail("Separate local reader did not observe the owned service")
        assert observed["snapshot"]["pid"]==process.pid and observed["history"]["summary"]["total"]==0
        process.stdin.write("stop\n"); process.stdin.flush()
        process.wait(timeout=10)
        output=process.stdout.read()
        assert process.returncode==0,output+process.stderr.read()
        assert '"event": "state"' in output
    finally:
        if process.poll() is None: process.terminate(); process.wait(timeout=5)
        for pipe in (process.stdin,process.stdout,process.stderr): pipe.close()


@pytest.mark.parametrize("stop_command", [True, False], ids=["stop-command", "owner-eof"])
def test_manager_host_starts_owned_strata_engine_before_stop(tmp_path, stop_command):
    """Real managed Python child and HTTP, with synthetic engine responses only."""
    path, value = configuration(tmp_path)
    with socket.socket() as api_probe, socket.socket() as engine_probe:
        api_probe.bind(("127.0.0.1", 0))
        engine_probe.bind(("127.0.0.1", 0))
        ports = [api_probe.getsockname()[1], engine_probe.getsockname()[1]]
    value["port"] = ports[0]
    path.write_text(json.dumps(value), encoding="utf-8")
    runtime = tmp_path / "runtime"
    (runtime / "serve").mkdir(parents=True)
    (runtime / "serve/server.py").write_text('''
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args): pass
HTTPServer(("127.0.0.1", int(sys.argv[-1])), Handler).serve_forever()
''', encoding="utf-8")
    bootstrap = tmp_path / "host.py"
    bootstrap.write_text('''
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ManagerActions as actions
import aiserver.app as application
from aiserver.inference.fake import FakeProvider
from aiserver.inference.strata import ManagedStrataEngine
value = json.loads(sys.stdin.readline())
engine = ManagedStrataEngine(value["engineConfig"], Path(value["config"]).parent / "logs")
engine.lock_directory = Path(value["config"]).parent / "engine-lock"
engine.record = engine.lock_directory / "process.json"
class Provider(FakeProvider):
    async def start(self): await engine.start()
    async def aclose(self):
        await engine.close()
        await super().aclose()
factory = application.create_app
application.create_app = lambda settings: factory(settings, provider=Provider(0))
asyncio.run(actions.host(value))
''', encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-u", str(bootstrap), str(ROOT / "scripts/server")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        env=support.child_environment(ROOT), creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name=="nt" else 0)
    job = None
    try:
        if os.name == "nt":
            from aiserver.operations.engine import WindowsJob, process_identity
            from aiserver.operations.processes import resume_suspended
            job = WindowsJob(process)
            resume_suspended(process)
        process.stdin.write(json.dumps({"config": str(path), "engineConfig": {
            "ExecutablePath": sys.executable, "EnginePath": sys.executable,
            "RuntimePath": str(runtime), "DependencyPath": str(runtime), "RunConfigPath": str(path),
            "BaseUrl": f"http://127.0.0.1:{ports[1]}", "StartupTimeoutSeconds": 5}}) + "\n")
        process.stdin.flush()
        ready = False
        until = time.monotonic() + 8
        with httpx.Client(trust_env=False) as client:
            while time.monotonic() < until and process.poll() is None:
                try:
                    ready = client.get(f'http://127.0.0.1:{ports[0]}/api/v1/health', timeout=.3).status_code == 200
                    if ready: break
                except httpx.HTTPError: pass
                time.sleep(.05)
        record = tmp_path / "engine-lock/process.json"
        engine_pid = json.loads(record.read_text(encoding="utf-8"))["pid"] if record.exists() else None
        if stop_command:
            process.stdin.write("stop\n"); process.stdin.flush()
        # communicate closes the owning input pipe, exercising EOF shutdown too.
        output, errors = process.communicate(timeout=12)
        assert ready, output + errors
        assert process.returncode == 0, output + errors
        assert engine_pid is not None and not record.exists()
        if os.name == "nt":
            with pytest.raises(ProcessLookupError): process_identity(engine_pid)
    finally:
        if job: job.close()
        if process.poll() is None: process.terminate(); process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr): pipe.close()


def _check_tk_window(tmp_path):
    import tkinter as tk
    import ServerManager as desktop
    path,value=configuration(tmp_path)
    window=tk.Tk(); window.withdraw()
    manager=desktop.ServerManager(window,preferences_file=tmp_path/"launcher.json",auto_load=False)
    try:
        manager.config.set(str(path))
        manager.apply_loaded(support.inspect_config(ROOT,path))
        assert len(manager.tabs.tabs())==5
        assert str(value["port"]) in manager.summary.get()
        assert manager.field_values()["manage_natapp"] is False
        manager.populate_users({"users":[{"user_id":"alice","display_name":"Alice","enabled":1}]})
        item=manager.users.get_children()[0]
        manager.users.selection_set(item)
        assert manager.selected_user()=="alice"
        assert tuple(manager.users["columns"])==("id","name","enabled")
        assert "login_token_days" not in manager.fields
        manager.show_created_key({"username":"alice","apiKey":"SYNTHETIC_DISPLAY_ONLY_KEY"})
        manager.show_keys({"username":"alice","keys":[{"key_id":"synthetic-id","created_at_ms":1,
                            "expires_at_ms":None,"status":"valid"}]})
        assert "SYNTHETIC_DISPLAY_ONLY_KEY" not in manager.logs.get("1.0","end")
        for child in window.winfo_children():
            if isinstance(child,tk.Toplevel): child.destroy()
        assert json.loads((tmp_path/"launcher.json").read_text(encoding="utf-8"))["config"]==str(path)
        assert not Path(value["data_directory"]).exists()
        manager.recommended()
        assert manager.field_values()["manage_natapp"] is True
        assert path.read_text(encoding="utf-8")==json.dumps(value,ensure_ascii=False)
        from types import SimpleNamespace
        from aiserver.settings import Settings
        from aiserver.storage.db import Store
        from aiserver.operations.engine import process_identity
        from aiserver.operations.monitoring import Monitor,performance_row,now_ms
        settings=Settings.load(path)
        monitor=Monitor(settings,Store(settings),SimpleNamespace(state="available"))
        monitor.identity=process_identity(os.getpid()); monitor.local_ready=True
        monitor.seen("alice")
        resources={"cpu_percent":40,"memory_used_bytes":2147483648,"memory_total_bytes":8589934592,
            "gpus":[{"index":"0","name":"Fixture GPU","utilization_percent":80,"memory_used_mib":1000,"memory_total_mib":16000}]}
        snapshot=monitor.snapshot(resources,[])
        at=now_ms()
        row=performance_row({"job_id":"fixture-job","user_id":"alice","status":"succeeded","error_code":None,
            "received_at_ms":at-21000,"created_at_ms":at-20000,"started_at_ms":at-18000,
            "first_output_at_ms":at-17000,"finished_at_ms":at,"input_tokens":100,"output_tokens":200,
            "load_ms":1000,"prompt_eval_ms":2000,"eval_ms":10000,"engine_total_ms":13000})
        history={"items":[row],"summary":{"total":1,"succeeded":1,"failed":0,"cancelled":0,"average_ms":21000,"p95_ms":21000},"page":1,"pages":1}
        manager.apply_monitor({"fresh":True,"snapshot":snapshot,"history":history})
        assert "活跃账号 1" in manager.monitor_counts.get() and "Fixture GPU" in manager.monitor_resources.get()
        assert manager.external_running and str(manager.start_button["state"])=="disabled"
        manager.start_service(); assert manager.child is None  # External viewer cannot create a competing child.
        manager.tasks.selection_set("fixture-job"); manager.task_details()
        assert "接收准备 1.00s" in manager.task_detail.get() and "模型加载 1.00s" in manager.task_detail.get()
        assert "P95 21.00s" in manager.monitor_summary.get()
        manager.mark_monitor_stale("实时数据已过期")
        assert not manager.external_running and "—" in manager.monitor_counts.get()
        manager.tabs.select(1)
        window.update_idletasks()
        # Keep the monitor table and detail inside the window at supported size.
        assert manager.tasks.winfo_reqheight()>0
    finally:
        manager.executor.shutdown(wait=True)
        manager.monitor_executor.shutdown(wait=True)
        window.destroy()


def _check_tk_service(tmp_path):
    import tkinter as tk
    import ServerManager as desktop
    path,value=configuration(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); value["port"]=sock.getsockname()[1]
    path.write_text(json.dumps(value),encoding="utf-8")
    window=tk.Tk(); window.withdraw()
    manager=desktop.ServerManager(window,preferences_file=tmp_path/"launcher.json",auto_load=False)
    try:
        manager.python.set(sys.executable)
        manager.config.set(str(path))
        manager.apply_loaded(support.inspect_config(ROOT,path))
        manager.start_service()
        until=time.monotonic()+20
        while time.monotonic()<until:
            window.update()
            if "就绪" in manager.status.get(): break
            time.sleep(0.02)
        else: pytest.fail("Tk service button did not start a Fake service: "+manager.status.get()+" / "+manager.feedback.get()+" / "+manager.logs.get("1.0","end"))
        assert manager.child is not None
        owner_pid=manager.child.pid
        manager.start_service()
        assert manager.child.pid==owner_pid and str(manager.start_button["state"])=="disabled"
        if os.name=="nt": assert manager.job is not None
        manager.request_monitor()
        until=time.monotonic()+6
        while manager.last_observed_ms is None and time.monotonic()<until:
            window.update(); time.sleep(.02)
        assert manager.last_observed_ms is not None,manager.monitor_status.get()
        assert "本窗口托管" in manager.monitor_status.get()
        manager.stop_service()
        until=time.monotonic()+10
        while manager.child and time.monotonic()<until:
            window.update(); time.sleep(0.02)
        assert manager.child is None and manager.job is None
        assert manager.status.get()=="服务已停止"
    finally:
        if manager.job: manager.job.close()
        if manager.child and manager.child.poll() is None:
            manager.child.terminate(); manager.child.wait(timeout=5)
        manager.executor.shutdown(wait=True)
        manager.monitor_executor.shutdown(wait=True)
        window.destroy()


def _isolated_tk(mode,tmp_path):
    result=subprocess.run([sys.executable,str(Path(__file__).resolve()),mode,str(tmp_path)],
        cwd=ROOT,text=True,encoding="utf-8",capture_output=True,env=support.child_environment(ROOT),timeout=50,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    assert result.returncode==0,result.stdout+result.stderr


def test_tk_window_tabs_config_and_user_binding(tmp_path):
    _isolated_tk("window",tmp_path)


def test_tk_service_buttons_use_owned_job_and_stop_cleanly(tmp_path):
    _isolated_tk("service",tmp_path)


if __name__=="__main__":
    (_check_tk_window if sys.argv[1]=="window" else _check_tk_service)(Path(sys.argv[2]))

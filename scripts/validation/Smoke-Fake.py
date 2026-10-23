"""Actual loopback HTTP + compiled C# Demo. Tokens exist only in process memory."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"server/src"))
import httpx
import uvicorn
from aiserver.app import create_app
from aiserver.settings import Settings

def main():
    run = ROOT/".local"/"smoke"/time.strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True)
    reports = ROOT/".local"/"reports"
    reports.mkdir(parents=True,exist_ok=True)
    settings=Settings(data_directory=run/"server",fake_delay_seconds=0.2)
    app=create_app(settings)
    store=app.state.store
    store.initialize()
    store.user_add("demo","Demo smoke")
    token=store.token_create("demo")  # never printed
    listener=socket.socket()
    listener.bind(("127.0.0.1",0)); listener.listen()
    port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,
                                       access_log=False,log_level="critical",proxy_headers=False))
    thread=threading.Thread(target=server.run,kwargs={"sockets":[listener]},daemon=True)
    thread.start()
    env=os.environ.copy()
    env["AISERVER_TOKEN"]=token
    env["AISERVER_BASE_URL"]="http://127.0.0.1:"+str(port)
    env["AISERVER_DEMO_DATA"]=str(run/"client")
    dll=ROOT/"tools/ClientDemo/bin/Debug/net8.0/ClientDemo.dll"
    evidence=[]
    def demo(*arguments):
        response=subprocess.run(["dotnet",str(dll),*arguments],env=env,cwd=ROOT,
                                capture_output=True,text=True,encoding="utf-8",timeout=45,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        if token in response.stdout+response.stderr:
            raise AssertionError("Token leaked")
        evidence.append({"command":list(arguments),"exitCode":response.returncode,
                         "stdout":response.stdout,"stderr":response.stderr})
        assert response.returncode==0, response.stdout+response.stderr
        return response
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:
            time.sleep(0.02)
        assert server.started
        demo("ready")
        for name in ("text-request","image-request"):
            data=json.loads((ROOT/"contracts/examples"/(name+".json")).read_text(encoding="utf-8"))
            data["clientRequestId"]=str(uuid4())
            if name == "text-request":
                first_request_id = data["clientRequestId"]
            path=run/(name+".json"); path.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
            demo("submit",str(path))
            demo("resume",data["clientRequestId"])
        data=json.loads((ROOT/"contracts/examples/text-request.json").read_text(encoding="utf-8"))
        data["clientRequestId"]=str(uuid4())
        with httpx.Client(base_url=env["AISERVER_BASE_URL"],trust_env=False,
                          headers={"Authorization":"Bearer "+token}) as client:
            accepted=client.post("/api/v1/jobs",json=data)
            assert accepted.status_code==202
            # Deliberately ignore accepted body; Demo knows only original request id.
            demo("resume",data["clientRequestId"])
        app.state.scheduler.provider.delay=10
        data["clientRequestId"]=str(uuid4())
        stop=run/"cancel-request.json"; stop.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
        demo("submit",str(stop),"--cancel-on-running")
        assert app.state.scheduler.provider.calls==4
        assert app.state.scheduler.provider.max_active==1
        with httpx.Client(base_url=env["AISERVER_BASE_URL"],trust_env=False,
                          headers={"Authorization":"Bearer "+token}) as client:
            items=client.get("/api/v1/jobs").json()["items"]
            assert sorted(x["status"] for x in items)==["cancelled","succeeded","succeeded","succeeded"]
        store.mark_recovery("smoke-fixture", "SMOKE_RECOVERING")
        demo("resume",first_request_id)  # Old results stay readable while new submissions are blocked.
        report={"layer":"Fake + actual loopback HTTP + C# Demo; not real model/public network/host",
                "python":sys.version,"provider":"Fake","modelCalls":4,"maxActive":1,
                "cases":evidence,"dataDirectory":str(run),"status":"passed"}
        (reports/"G0-G2-demo-smoke.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("PASS: C# ready, text/image submit and resume, ignored submission response recovery, running cancel; 4 calls, max active 1.")
        print("Evidence:",reports/"G0-G2-demo-smoke.json")
    finally:
        server.should_exit=True
        thread.join(timeout=40)
        listener.close()
        assert not thread.is_alive()

if __name__=="__main__":
    main()

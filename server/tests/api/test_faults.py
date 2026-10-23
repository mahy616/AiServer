from dataclasses import replace
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from aiserver.app import create_app
from aiserver.inference.fake import FakeProvider, fake_profile
from aiserver.settings import Settings
from test_fake_api import service, request, post, terminal

def test_global_capacity_five_users(tmp_path):
    provider=FakeProvider(30)
    with service(tmp_path,provider=provider) as (client,store,scheduler,other):
        headers=[]
        for index in range(5):
            user="u"+str(index)
            store.user_add(user,user)
            headers.append({"Authorization":"Bearer "+store.token_create(user)})
        for user_headers in headers:
            for _ in range(2):
                response=client.post("/api/v1/jobs",json=request(),headers=user_headers)
                assert response.status_code==202,response.text
        # alice has no jobs but global capacity is already occupied.
        response=client.post("/api/v1/jobs",json=request())
        assert response.status_code==429 and response.json()["error"]["code"]=="QUEUE_FULL"
        assert provider.max_active==1

def test_sqlite_write_failure_never_returns_accepted(tmp_path):
    with service(tmp_path,fake_delay_seconds=0) as (client,store,scheduler,other):
        with store.connection(True):
            response=client.post("/api/v1/jobs",json=request())
            assert response.status_code==503 and response.json()["error"]["code"]=="STORAGE_UNAVAILABLE"
        with store.connection() as c:
            assert c.execute("SELECT count(*) FROM jobs").fetchone()[0]==0
        assert scheduler.provider.calls==0

def test_unverified_cleanup_recovers_instead_of_next_generation(tmp_path):
    cfg=Settings(data_directory=tmp_path)
    profile=replace(fake_profile(cfg),automatic_cleanup_verified=False)
    provider=FakeProvider(30)
    with service(tmp_path,provider=provider,profile=profile) as (client,store,scheduler,other):
        first=post(client,request())
        client.portal.call(provider.started.wait)
        client.post("/api/v1/jobs/"+first+"/cancel")
        assert terminal(client,first)["status"]=="cancelled"
        until=time.monotonic()+2
        while scheduler.state!="recovering" and time.monotonic()<until:
            time.sleep(0.01)
        assert scheduler.state=="recovering"
        assert client.post("/api/v1/jobs",json=request()).status_code==503
        assert provider.calls==1

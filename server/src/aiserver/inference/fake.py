"""Development fixtures; byte limits here have NO relationship to model tokens."""
import asyncio
import hashlib
import json

from .contracts import BudgetCheck, Generation, ModelError, PreparedInput, Profile


class FakeBudget:
    def __init__(self, max_bytes=131072, verified=True):
        self.max_bytes, self.verified = max_bytes, verified
        self.accepted_input_types = ("text", "image") if verified else ()

    async def prepare(self, task, profile):
        if not self.verified:
            return BudgetCheck(code="INPUT_BUDGET_UNVERIFIED")
        request = json.loads(task.request_json)
        text_bytes = sum(len(p["text"].encode("utf-8")) for m in request["messages"]
                         for p in m["content"] if p["type"] == "text")
        if text_bytes > self.max_bytes:
            return BudgetCheck(code="CONTEXT_TOO_LARGE")
        return BudgetCheck(PreparedInput(profile.profile_id, task.request_json,
                                        hashlib.sha256(task.request_json.encode()).hexdigest(),
                                        "fake-bytes-only-v1", "client-messages-v1"))


class UnverifiedBudget:
    accepted_input_types = ()
    async def prepare(self, task, profile):
        return BudgetCheck(code="INPUT_BUDGET_UNVERIFIED")


def fake_profile(settings):
    snapshot = json.dumps({"provider": "Fake", "profileId": "fake-v1",
                           "delay": settings.fake_delay_seconds, "scenario": settings.fake_scenario,
                           "maxMaterialBytes": settings.fake_max_material_bytes,
                           "budgetMethod": "fake-bytes-only-v1"}, sort_keys=True)
    return Profile("fake-v1", "Fake", snapshot, ("text", "image"), True)


class FakeProvider:
    supports_tools = True  # Transport fixture only; not evidence of real-model tool quality.
    def __init__(self, delay=1, scenario="success", late_seconds=0.1):
        self.delay, self.scenario, self.late_seconds = delay, scenario, late_seconds
        self.started = asyncio.Event()
        self.release = None
        self.calls = self.active = self.max_active = 0

    async def generate(self, prepared, stop, on_delta):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            request = json.loads(prepared.payload_json)
            text = "FAKE transport fixture: " + "".join(p["text"] for m in request["messages"]
                         if m["role"] == "user" for p in m["content"] if p["type"] == "text")
            if self.scenario == "empty":
                text = ""
            if text:
                await on_delta(text[:len(text)//2])
            try:
                if self.release is not None:
                    await self.release.wait()
                else:
                    await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                if self.scenario != "late":
                    raise
                await asyncio.sleep(self.late_seconds)
            if self.scenario in ("failure", "protocol", "connection"):
                raise ModelError({"failure": "MODEL_UNAVAILABLE", "protocol": "MODEL_PROTOCOL_ERROR",
                                  "connection": "MODEL_UNAVAILABLE"}[self.scenario],
                                 request_finished=self.scenario == "failure")
            if text:
                await on_delta(text[len(text)//2:])
            return Generation(text, self.scenario != "truncated",
                              "length" if self.scenario == "truncated" else "stop")
        finally:
            self.active -= 1

    async def aclose(self):
        pass

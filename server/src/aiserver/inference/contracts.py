"""Internal inference contract v3.

Prepare immutable client messages before insertion; never rebuild them in workers.
Profile snapshots freeze model identity, mapping, capacity and server parameters.
Providers await on_delta(text, channel); both channels commit before SSE sends them.
Cancellation closes transport. Late returns are discarded; the slot stays held.
automatic_cleanup_verified is used only for development test providers.
Real providers verify upstream idleness or restart their managed engine before reuse.
Python task termination alone never certifies GPU stop.
"""
from dataclasses import dataclass
from typing import Protocol
import asyncio

CONTRACT_VERSION = 3

class DeltaSink(Protocol):
    async def __call__(self, text: str, channel: str = "content") -> None: ...

@dataclass(frozen=True)
class ImageInput:
    image_id: str
    data: bytes
    sha256: str
    width: int
    height: int
    media_type: str = "image/png"

@dataclass(frozen=True)
class TaskInput:
    request_json: str
    images: tuple[ImageInput, ...]

@dataclass(frozen=True)
class Profile:
    profile_id: str
    provider: str
    snapshot_json: str
    accepted_input_types: tuple[str, ...]
    automatic_cleanup_verified: bool = False

@dataclass(frozen=True)
class PreparedInput:
    profile_id: str
    payload_json: str
    material_sha256: str
    budget_method: str
    prompt_version: str
    input_tokens: int | None = None
    estimated_input_tokens: int | None = None

@dataclass(frozen=True)
class BudgetCheck:
    prepared: PreparedInput | None = None
    code: str | None = None
    details: dict | None = None

@dataclass(frozen=True)
class Generation:
    text: str
    is_complete: bool
    finish_reason: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Allowlisted engine durations in milliseconds; never raw engine responses.
    engine_metrics: dict | None = None
    tool_calls: tuple[dict, ...] = ()
    reasoning_text: str = ""

class ModelError(Exception):
    def __init__(self, code: str, request_finished: bool = False):
        super().__init__(code)
        self.code = code
        self.request_finished = request_finished

class Budget(Protocol):
    # Supported input types; capacityMethod distinguishes estimates from exact counts.
    accepted_input_types: tuple[str, ...]

    async def prepare(self, task: TaskInput, profile: Profile) -> BudgetCheck: ...

class Provider(Protocol):
    async def generate(self, prepared: PreparedInput, stop: asyncio.Event, on_delta: DeltaSink) -> Generation: ...
    async def aclose(self) -> None: ...

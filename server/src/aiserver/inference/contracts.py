"""A/B internal contract v1, maintained by A.

prepare() precedes insertion, outside SQL. Persist its exact prepared payload;
never rebuild prompts in workers. Profile snapshot includes actual identity,
digest/template/prompt/budget/image policy/parameters, without credentials.
Cancellation: A sets stop then cancels the task; provider closes transport and
raises CancelledError. Late returns are discarded; slot held until termination.
automatic_cleanup_verified is used only for development test providers.
Real providers restart their managed engine to establish a fresh execution slot.
Python task termination alone never certifies GPU stop.
"""
from dataclasses import dataclass
from typing import Protocol
import asyncio

CONTRACT_VERSION = 1

@dataclass(frozen=True)
class ImageInput:
    image_id: str
    png: bytes
    sha256: str
    width: int
    height: int

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

@dataclass(frozen=True)
class Generation:
    text: str
    is_complete: bool
    finish_reason: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None

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
    async def generate(self, prepared: PreparedInput, stop: asyncio.Event) -> Generation: ...
    async def aclose(self) -> None: ...

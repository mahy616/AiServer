from pathlib import Path
import json
import os
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from aiserver.operations.urls import public_url

ROOT = Path(__file__).resolve().parents[3]

class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str = "Development"
    provider: str = "Fake"
    inference_config: Path = ROOT / "config" / "inference.qwen35.example.json"
    additional_inference_configs: list[Path] = Field(default_factory=list, max_length=7)
    # Deprecated configuration key accepted for upgrades; never injected into new requests.
    system_prompt: Path | None = None
    manage_engine: bool = True
    data_directory: Path = ROOT / ".local" / "server"
    contract_directory: Path = ROOT / "contracts" / "api" / "v1"
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]
    port: int = Field(default=18080, ge=1024, le=65535)
    public_base_url: str = ""
    max_request_bytes: int = Field(default=20971520, gt=0, le=20971520)
    upload_timeout_seconds: float = Field(default=120, gt=0)
    max_outstanding_global: int = Field(default=10, ge=1, le=100)
    max_outstanding_per_user: int = Field(default=2, ge=1, le=10)
    queue_timeout_seconds: float = Field(default=1800, gt=0)
    execution_timeout_seconds: float = Field(default=600, gt=0)
    cleanup_timeout_seconds: float = Field(default=30, gt=0)
    poll_seconds: int = Field(default=3, ge=1)
    payload_days: int = Field(default=1, ge=1)
    tombstone_days: int = Field(default=30, ge=1)
    fake_delay_seconds: float = Field(default=1, ge=0)
    fake_scenario: str = "success"
    fake_late_seconds: float = Field(default=0.1, ge=0)
    fake_max_material_bytes: int = Field(default=131072, ge=1)
    requests_per_minute: int = Field(default=120, ge=1)
    submissions_per_minute: int = Field(default=6, ge=1)
    public_requests_per_minute: int = Field(default=60, ge=1)
    log_max_bytes: int = Field(default=5242880, ge=1024)
    log_backup_count: int = Field(default=5, ge=1)
    stream_heartbeat_seconds: float = Field(default=10, gt=0)
    stream_send_timeout_seconds: float = Field(default=30, gt=0)
    manage_natapp: bool | None = None
    natapp_executable: Path = ROOT / ".local" / "natapp" / "natapp.exe"
    natapp_config: Path = ROOT / ".local" / "natapp" / "config.ini"
    natapp_restart_initial_seconds: float = Field(default=2, gt=0)
    natapp_restart_max_seconds: float = Field(default=60, ge=1)
    service_startup_timeout_seconds: float = Field(default=90, gt=0)

    @model_validator(mode="before")
    @classmethod
    def discard_retired_login_settings(cls, values):
        if isinstance(values, dict):
            values = {key: value for key, value in values.items() if key not in (
                "login_token_days", "login_attempts_per_minute", "login_attempts_per_username_per_minute")}
        return values

    @field_validator("public_base_url",mode="before")
    @classmethod
    def check_public_url(cls, value):
        return public_url(value)

    @model_validator(mode="after")
    def check(self):
        if self.provider not in ("Fake", "Ollama") or self.environment not in ("Development", "Production"):
            raise ValueError("Unknown environment/provider")
        if self.environment == "Production" and self.provider == "Fake":
            raise ValueError("Fake is restricted to Development")
        if self.manage_natapp is None:
            self.manage_natapp = self.environment == "Production"
        if self.natapp_restart_max_seconds < self.natapp_restart_initial_seconds:
            raise ValueError("Invalid tunnel restart bounds")
        if self.fake_scenario not in ("success", "late", "failure", "empty", "truncated", "protocol", "connection"):
            raise ValueError("Invalid Fake scenario")
        if not self.data_directory.is_absolute() or not self.contract_directory.is_absolute():
            raise ValueError("Data/contract paths must be absolute")
        self.additional_inference_configs = [path if path.is_absolute() else ROOT / path
                                             for path in self.additional_inference_configs]
        if self.max_outstanding_per_user > self.max_outstanding_global or self.payload_days > self.tombstone_days:
            raise ValueError("Invalid limits")
        return self

    @classmethod
    def load(cls, path: str | None = None):
        values = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else {}
        for key in cls.model_fields:
            raw = os.getenv("AISERVER_" + key.upper())
            if raw is not None:
                try:
                    values[key] = json.loads(raw)
                except ValueError:
                    values[key] = raw
        # Portable templates resolve against the release root, never the shell cwd.
        for key in ("data_directory", "contract_directory", "inference_config", "system_prompt",
                    "natapp_executable", "natapp_config"):
            if key in values and values[key] is not None and not Path(values[key]).is_absolute():
                values[key] = ROOT / values[key]
        return cls(**values)

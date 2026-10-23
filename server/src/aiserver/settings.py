from pathlib import Path
import json
import os
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[3]

class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str = "Development"
    provider: str = "Fake"
    inference_config: Path = ROOT / "config" / "inference.development.example.json"
    system_prompt: Path = ROOT / "config" / "prompts" / "s01-bootstrap-vl-v2.txt"
    manage_engine: bool = True
    data_directory: Path = ROOT / ".local" / "server"
    contract_directory: Path = ROOT / "contracts" / "api" / "v1"
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]
    port: int = Field(default=18080, ge=1024, le=65535)
    max_request_bytes: int = Field(default=5242880, gt=0, le=5242880)
    upload_timeout_seconds: float = Field(default=120, gt=0)
    max_outstanding_global: int = Field(default=10, ge=1, le=100)
    max_outstanding_per_user: int = Field(default=2, ge=1, le=10)
    queue_timeout_seconds: float = Field(default=1800, gt=0)
    execution_timeout_seconds: float = Field(default=600, gt=0)
    cleanup_timeout_seconds: float = Field(default=30, gt=0)
    poll_seconds: int = Field(default=3, ge=1)
    payload_days: int = Field(default=7, ge=1)
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

    @model_validator(mode="after")
    def check(self):
        if self.provider not in ("Fake", "Ollama") or self.environment not in ("Development", "Production"):
            raise ValueError("Unknown environment/provider")
        if self.environment == "Production" and self.provider == "Fake":
            raise ValueError("Fake is restricted to Development")
        if self.fake_scenario not in ("success", "late", "failure", "empty", "truncated", "protocol", "connection"):
            raise ValueError("Invalid Fake scenario")
        if not self.data_directory.is_absolute() or not self.contract_directory.is_absolute():
            raise ValueError("Data/contract paths must be absolute")
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
        for key in ("data_directory", "contract_directory", "inference_config", "system_prompt"):
            if key in values and not Path(values[key]).is_absolute():
                values[key] = ROOT / values[key]
        return cls(**values)

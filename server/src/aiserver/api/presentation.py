from datetime import datetime, timezone
import json
from .errors import safe_error
from aiserver.storage.db import TERMINAL, now_ms

def iso(at):
    return datetime.fromtimestamp(at/1000,timezone.utc).isoformat().replace("+00:00","Z") if at is not None else None

def available(row):
    return row["status"] in TERMINAL and row["payload_purged_at_ms"] is None and (
        row["payload_expires_at_ms"] or 0) > now_ms()

def status_response(row, state, queue, poll):
    created, started, finished = (row[key] for key in ("created_at_ms","started_at_ms","finished_at_ms"))
    return {"jobId":row["job_id"],"clientRequestId":row["client_request_id"],"status":row["status"],
            "createdAt":iso(created),"startedAt":iso(started),"finishedAt":iso(finished),
            "queue":queue, "timing":{"queueMs":max(0,(started or finished or now_ms())-created),
                                    "executionMs":max(0,(finished or now_ms())-started) if started else None},
            "schedulerState":state,"pollAfterSeconds":poll,"resultAvailable":available(row),
            "payloadExpiresAt":iso(row["payload_expires_at_ms"]),
            "error":safe_error(row["error_code"]) if row["error_code"] else None}

def result_response(row):
    request = json.loads(row["request_json"])
    ctx = request["context"]
    import hashlib
    code = request["currentCode"]
    generation = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
    effective = json.loads(row["effective_settings_json"])
    generation.update({"profileId":row["profile_id"],"promptVersion":effective["prepared"]["prompt_version"],
                       "inferenceProvider":effective["profile"]["provider"],
                       "estimatedInputTokens":effective["prepared"].get("estimated_input_tokens"),
                       "capacityMethod":effective["prepared"]["budget_method"]})
    return {"jobId":row["job_id"],"status":row["status"],
            "output":json.loads(row["output_json"]) if row["output_json"] else None,
            "validation":{"metadata":"not_run","compilation":"not_run","business":"not_run"},
            "source":{key:ctx[key] for key in ("projectId","scriptId","snapshotRevision")} |
                     {"currentCodeSha256":hashlib.sha256(code.encode("utf-8")).hexdigest() if code else None},
            "generation":generation,"error":safe_error(row["error_code"]) if row["error_code"] else None,
            "payloadExpiresAt":iso(row["payload_expires_at_ms"])}

"""Capacity search and guarded configuration updates; no engine or network access."""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


class CapacityFailure(RuntimeError):
    """A measured allocation/offload boundary, not an arbitrary engine failure."""


def largest_context(minimum, maximum, step, probe):
    """Return the largest passing grid point under a monotonic capacity limit."""
    low, high = (minimum + step - 1) // step, maximum // step
    if low > high:
        raise ValueError("No context fits the search range")
    if probe(high * step):
        return high * step
    if low == high or not probe(low * step):
        raise CapacityFailure("MINIMUM_CONTEXT_FAILED")
    # low passes, high fails. Unknown failures must raise, never return False.
    while high - low > 1:
        middle = (low + high) // 2
        if probe(middle * step):
            low = middle
        else:
            high = middle
    return low * step


def commit_configs(plans, before, support, unchanged, *, backup_label="capacity"):
    """Back up all files, preserve concurrent edits, roll back only our writes."""
    unchanged(before)
    changes = []
    for target, value in plans:
        raw = before[target]
        if json.loads(raw.decode("utf-8-sig")) == value:
            continue
        backup = target.with_name(target.name + ".before-" + backup_label + "-" +
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6] + ".bak")
        with backup.open("xb") as stream:
            stream.write(raw)
        changes.append((target, raw, (json.dumps(value, ensure_ascii=False, indent=2)+"\n").encode("utf-8"), backup))
    written, expected = [], dict(before)
    try:
        for target, raw, updated, backup in changes:
            unchanged(expected)
            support.atomic_write(target, updated)
            written.append((target, raw, updated))
            expected[target] = updated
        unchanged(expected)
    except BaseException:
        for target, raw, updated in reversed(written):
            if target.is_file() and target.read_bytes() == updated:
                support.atomic_write(target, raw)
        raise
    return [{"config": str(target), "backup": str(backup)} for target, _, _, backup in changes]


def write_report(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.replace(path)

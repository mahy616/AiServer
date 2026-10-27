"""Real remote inference and local read-tool roundtrip, using only the independent key."""
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from uuid import uuid4

from opencode_support import ROOT, WORK, BINARY, environment, refresh_connection, atomic_write, json_bytes


def main():
    work = WORK / "verification" / uuid4().hex
    work.mkdir(parents=True)
    summary = {"passed": False, "work": str(work), "credential": "independent-api-key"}
    try:
        connection = refresh_connection()
        proof = "LOCAL_READ_PROOF_" + uuid4().hex
        marker = work / "proof.txt"
        marker.write_text(proof + "\n", encoding="utf-8")
        version = tomllib.loads((ROOT / "server/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        prompt = ("Perform a read-only verification. You MUST use the read tool to inspect these local files: "
            + str(ROOT / "README.md") + ", " + str(ROOT / "server/pyproject.toml") + ", and " + str(marker)
            + ". Read only these files; do not modify files, run commands, or delegate. "
            "In your final answer report the server project version from pyproject.toml, the exact first line "
            "of proof.txt, and one sentence describing AiServer. Do not guess the proof; read it with a tool.")
        command = [str(BINARY), "run", "--pure", "--agent", "build", "--format", "json",
                   "--title", "AiServer independent key tool verification", prompt]
        with (work / "events.jsonl").open("w", encoding="utf-8") as output, (work / "stderr.txt").open("w", encoding="utf-8") as errors:
            try:
                result = subprocess.run(command, cwd=ROOT, env=environment(True), stdout=output, stderr=errors, timeout=300)
            except subprocess.TimeoutExpired:
                summary["error"] = "OpenCode verification exceeded 300 seconds"
                return 1
        events = []
        for line in (work / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        tools = [e for e in events if e.get("type") == "tool_use"]
        # Tool payloads contain the proof too; only a final model answer proves the return trip.
        answer = "\n".join(e.get("part", {}).get("text", "") for e in events if e.get("type") == "text")
        (work / "answer.txt").write_text(answer, encoding="utf-8")
        read_paths = [e.get("part", {}).get("state", {}).get("input", {}).get("filePath", "") for e in tools]
        expected = {str(ROOT / "README.md").casefold(), str(ROOT / "server/pyproject.toml").casefold(), str(marker).casefold()}
        completed = all(e.get("part", {}).get("tool") == "read" and
                        e.get("part", {}).get("state", {}).get("status") == "completed" for e in tools)
        finishes = [e.get("part", {}).get("reason") for e in events if e.get("type") == "step_finish"]
        summary.update(exit_code=result.returncode, connection=connection, tool_count=len(tools),
                       only_completed_reads=bool(tools) and completed,
                       expected_files_read=expected.issubset({str(Path(p)).casefold() for p in read_paths}),
                       proof_in_final_response=proof in answer, version_in_final_response=version in answer,
                       model_turns=len(finishes), tool_roundtrip="tool-calls" in finishes and finishes[-1:] == ["stop"],
                       usage=[e["part"].get("tokens") for e in events if e.get("type") == "step_finish"])
        summary["passed"] = result.returncode == 0 and all(summary[k] for k in (
            "only_completed_reads", "expected_files_read", "proof_in_final_response", "version_in_final_response", "tool_roundtrip"))
        return 0 if summary["passed"] else 1
    except Exception as error:
        # Never serialize request objects or tracebacks containing authentication headers.
        summary["error"] = type(error).__name__
        return 1
    finally:
        atomic_write(work / "summary.json", json_bytes(summary))
        atomic_write(WORK / "verification/last-result.json", json_bytes(summary))
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

"""Small REAL-model check on a temporary API/database/engine, never the public service.

Requires Install-Qwen25Coder7B.py --prepare-only first. Uses a 4K window by
default for small development GPUs; it does not certify 5080 capacity or quality.
"""
import argparse
import json
from pathlib import Path
import socket
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))


def main():
    from fastapi.testclient import TestClient
    from aiserver.app import create_app
    from aiserver.settings import Settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inference", type=Path, default=ROOT / "config/inference.coder.local.json")
    parser.add_argument("--context", type=int, default=4096)
    args = parser.parse_args()
    cfg = json.loads(args.inference.read_text(encoding="utf-8-sig"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        cfg["BaseUrl"] = f"http://127.0.0.1:{probe.getsockname()[1]}"
    cfg.update(NumCtx=args.context,
               NumPredict=256, ReservedTokens=512, NumBatch=128)
    work = ROOT / ".local/coder/smoke" / uuid4().hex
    work.mkdir(parents=True)
    path = work / "inference.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    settings = Settings(provider="Ollama", environment="Development", manage_natapp=False,
                        data_directory=work / "data", inference_config=path,
                        execution_timeout_seconds=3600, submissions_per_minute=60)
    app = create_app(settings)
    store = app.state.store
    store.initialize()
    store.user_add("smoke", "Local smoke")
    token = store.token_create("smoke")
    summary = {"model": cfg["Model"], "context": cfg["NumCtx"], "batch": cfg["NumBatch"],
               "kv": cfg["KvCacheType"], "checks": []}
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + token
        assert client.get("/api/v1/ready").json()["canAcceptJobs"]
        def chat(messages, **options):
            response = client.post("/v1/chat/completions", json={"model": cfg["Model"], "messages": messages, **options})
            if response.status_code != 200:
                raise RuntimeError("Chat failed: " + str(response.status_code) + " " + response.text[:500])
            return response
        messages = [{"role": "user", "content": "Reply with exactly OK."}]
        answer = chat(messages).json()
        assert answer["choices"][0]["message"]["content"]
        summary["checks"].append({"name": "text", "usage": answer["usage"]})
        streamed = chat(messages, stream=True, stream_options={"include_usage": True})
        assert "data: [DONE]" in streamed.text and '"content":' in streamed.text
        summary["checks"].append({"name": "stream", "passed": True})
        tool = {"type": "function", "function": {"name": "read_file", "description": "Read a local text file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
        messages = [{"role": "user", "content": "Use read_file to read example.txt. You must call the tool first."}]
        answer = chat(messages, tools=[tool], tool_choice="required").json()
        assistant = answer["choices"][0]["message"]
        calls = assistant.get("tool_calls")
        assert calls and calls[0]["function"]["name"] == "read_file"
        messages.append(assistant)
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": "The file contains: LOCAL_TOOL_OK"})
        answer = chat(messages).json()
        assert answer["choices"][0]["message"]["content"]
        summary["checks"].append({"name": "tool_roundtrip", "usage": answer["usage"]})
        with store.connection() as connection:
            rows = connection.execute("SELECT effective_settings_json,metrics_json FROM job_payloads").fetchall()
        summary["token_counts"] = [{"admission": json.loads(row[0])["prepared"]["estimated_input_tokens"],
                                    "engine": json.loads(row[1])["inputTokens"]} for row in rows]
        assert all(item["admission"] == item["engine"] for item in summary["token_counts"])
    (work / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print("Logs and summary: " + str(work))


if __name__ == "__main__":
    main()

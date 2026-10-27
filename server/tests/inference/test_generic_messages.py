import asyncio
import base64
from io import BytesIO
import json

import httpx
from PIL import Image

from aiserver.api.validation import RequestValidator, canonical
from aiserver.context.prompt_builder import prepare_messages
from aiserver.inference.ollama import OllamaAdapter
from aiserver.settings import ROOT


def test_ordered_native_roles_history_images_without_injected_prompt():
    images = []
    for format in ("PNG", "JPEG"):
        data = BytesIO(); Image.new("RGB", (4, 4), "red").save(data, format=format)
        images.append({"type":"image", "mediaType":"image/png" if format == "PNG" else "image/jpeg",
                       "dataBase64":base64.b64encode(data.getvalue()).decode()})
    messages = [
        {"role":"user","content":[{"type":"text","text":"old request"}]},
        {"role":"assistant","content":[{"type":"text","text":"old answer"}]},
        {"role":"user","content":[{"type":"text","text":"before"},images[0],
                                    {"type":"text","text":"between"},images[1],
                                    {"type":"text","text":"after"}]}]
    request = {"clientRequestId":"416417b5-e936-4cde-b0df-969281936318",
               "model":"fixture", "messages":messages}
    normalized, fingerprint, task = RequestValidator(ROOT/"contracts/api/v1").validate(canonical(request).encode())
    policy = json.loads((ROOT/"config/inference.5080.example.json").read_text())["ImagePolicy"]
    policy["MaxImages"] = 4
    prepared = prepare_messages(normalized, task.images, policy)
    native = prepared.messages()
    assert [m["role"] for m in native] == ["user", "assistant", "user"]
    assert native[-1]["content"] == "before[img]between[img]after"
    assert native[-1]["images"] == [image["dataBase64"] for image in images]
    assert native[1]["content"] == "old answer"


def test_ndjson_split_utf8_is_published_before_engine_finishes():
    async def check():
        cfg = json.loads((ROOT/"config/inference.development.example.json").read_text())
        adapter = OllamaAdapter(cfg)
        await adapter.client.aclose()
        first = json.dumps({"model":cfg["Model"],"done":False,"message":{"content":"中文🙂"}}, ensure_ascii=False).encode()+b"\n"
        final = json.dumps({"model":cfg["Model"],"done":True,"done_reason":"stop","message":{"content":""}}).encode()+b"\n"
        published = asyncio.Event()
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index in range(0, len(first), 2):
                    yield first[index:index+2]
                await asyncio.wait_for(published.wait(), 1)
                yield final
        adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"],transport=httpx.MockTransport(
            lambda req:httpx.Response(200,stream=Chunks())))
        from aiserver.context.prompt_builder import PreparedPrompt
        prompt = PreparedPrompt('[]', "unused", "client-messages-v1", (), 0)
        parts = []
        async def delta(text):
            parts.append(text); published.set()
        try:
            result = await adapter.chat(prompt, delta)
            assert parts == ["中文🙂"] and result.text == "中文🙂" and result.is_complete
        finally:
            await adapter.aclose()
    asyncio.run(check())

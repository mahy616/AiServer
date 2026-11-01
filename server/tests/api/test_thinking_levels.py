"""Named controls through public admission, frozen Ollama payloads and capability discovery."""
import json
from uuid import uuid4

import pytest

from aiserver.context.prompt_builder import PreparedPrompt
from aiserver.inference.configuration import chat_payload
from aiserver.inference.contracts import Generation, Profile
from aiserver.inference.fake import FakeProvider
from aiserver.inference.runtime import PracticalBudget
from aiserver.settings import ROOT
from test_fake_api import service, assert_schema


class LevelProvider(FakeProvider):
    supports_thinking = True

    def __init__(self, cfg):
        super().__init__(0)
        self.cfg, self.payloads = cfg, []

    async def generate(self, prepared, stop, on_delta):
        self.calls += 1
        raw = json.loads(prepared.payload_json)
        raw['image_sha256'] = tuple(raw['image_sha256'])
        prompt = PreparedPrompt(**raw)
        assert prompt.material_sha256() == prepared.material_sha256
        self.payloads.append(chat_payload(self.cfg, prompt, stream=True))
        await on_delta('synthetic reasoning', 'reasoning_content')
        await on_delta('synthetic answer')
        return Generation('synthetic answer', True, 'stop', 20, 10, reasoning_text='synthetic reasoning')


def setup_model(key='gptoss'):
    cfg = json.loads((ROOT / f'config/inference.{key}.example.json').read_bytes())
    profile = Profile(key, 'Ollama', json.dumps({'config': cfg}), ('text',))
    return cfg, profile, PracticalBudget(cfg, supports_thinking=True), LevelProvider(cfg)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('control,expected', [({}, 'medium'), ({'think': None}, 'medium'),
    ({'think': True}, 'medium'), ({'think': 'low'}, 'low'), ({'think': 'high'}, 'high'),
    ({'reasoning_effort': 'medium'}, 'medium'), ({'think': 'low', 'reasoning_effort': 'low'}, 'low')])
def test_named_controls_reach_frozen_engine_payload_and_schema(tmp_path, stream, control, expected):
    cfg, profile, budget, provider = setup_model()
    with service(tmp_path, profile=profile, budget=budget, provider=provider) as (client, *_):
        models = client.get('/api/v1/models').json()
        assert_schema('models', models)
        assert models['models'][0]['thinkingLevels'] == ['low', 'medium', 'high']
        assert models['models'][0]['defaultThinkingLevel'] == 'medium'
        assert models['models'][0]['thinkingCanDisable'] is False
        response = client.post('/v1/chat/completions', json={'model': cfg['Model'],
            'messages': [{'role': 'user', 'content': 'test'}], 'stream': stream, **control})
        assert response.status_code == 200, response.text
        assert provider.payloads[-1]['think'] == expected
        assert provider.payloads[-1]['options']['num_predict'] == 4096
        assert provider.payloads[-1]['shift'] is False and provider.payloads[-1]['truncate'] is False
        assert ('[DONE]' in response.text) if stream else response.json()['choices'][0]['message']['content'] == 'synthetic answer'


@pytest.mark.parametrize('key,control', [('gptoss', {'think': False}), ('gptoss', {'think': 'ultra'}),
    ('gptoss', {'think': 'low', 'reasoning_effort': 'high'}), ('gptoss', {'think': 1}),
    ('gemma12', {'think': 'low'}), ('gemma26', {'reasoning_effort': 'high'})])
def test_invalid_model_controls_fail_before_generation(tmp_path, key, control):
    cfg, profile, budget, provider = setup_model(key)
    with service(tmp_path, profile=profile, budget=budget, provider=provider) as (client, *_):
        response = client.post('/v1/chat/completions', json={'model': cfg['Model'],
            'messages': [{'role': 'user', 'content': 'test'}], **control})
        assert response.status_code == 400, response.text
        assert provider.calls == 0


def test_native_named_level_uses_same_policy(tmp_path):
    cfg, profile, budget, provider = setup_model()
    with service(tmp_path, profile=profile, budget=budget, provider=provider) as (client, *_):
        response = client.post('/api/v1/jobs', json={'clientRequestId': str(uuid4()), 'model': cfg['Model'],
            'think': 'high', 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'test'}]}]})
        assert response.status_code == 200, response.text
        assert provider.payloads[-1]['think'] == 'high'

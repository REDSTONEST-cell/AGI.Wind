import asyncio
import json
from types import SimpleNamespace

import pytest

from agi_wind.core.types import ModelRef
from agi_wind.integrations.qq_attention import GroupAttention
from test_qq_attention import message


class Classifier:
    def __init__(self, result='{"addressed":true,"confidence":0.95}', callback=None):
        self.result, self.callback, self.calls = result, callback, []
        self.config = SimpleNamespace(supported_parameters=['max_output_tokens', 'temperature', 'response_format'])

    async def complete(self, model, messages):
        self.calls.append((model, messages))
        if self.callback:
            self.callback()
        return self.result


MODEL = ModelRef(provider_id='fake', model_id='test', parameters={'tools': ['ignored'], 'max_output_tokens': 9999})


async def test_strong_signals_and_known_participant_followup_are_free_but_other_members_are_not():
    attention = GroupAttention(); attention.mark_replied('room-1', 'member-1')
    provider = Classifier()
    for msg in [message(mentioned_qq_ids=['10002']), message(reply_to_platform_message_id='9'), message('那后来呢')]:
        result = await attention.decide_smart(msg, receipt_exists=True, provider=provider, model=MODEL)
        assert result.action == 'reply'
    assert not provider.calls
    result = await attention.decide_smart(message('为什么', sender_principal_id='member-2'), provider=provider, model=MODEL)
    assert result.action == 'reply'; assert len(provider.calls) == 1
    assert attention.decide(message('为什么', sender_principal_id='member-2')).action == 'observe'


async def test_participant_ambiguity_is_bounded_and_context_is_untrusted_data():
    attention = GroupAttention(); attention.mark_replied('room-1', 'member-1')
    provider = Classifier()
    result = await attention.decide_smart(message('这个安排放在明天下午' * 150), provider=provider, model=MODEL,
                                         context=[{'text': '上下文' * 200, 'sender_principal_id': str(i)} for i in range(12)])
    assert result.action == 'reply'
    model, prompts = provider.calls[0]
    assert model.parameters == {'max_output_tokens': 128, 'temperature': 0, 'response_format': {'type': 'json_object'}}
    payload = json.loads(prompts[1]['content'])
    assert len(payload['message']['text']) <= 500
    assert len(payload['context']) == 6
    assert all(len(item['text']) <= 200 for item in payload['context'])
    assert prompts[0]['role'] == 'system'


@pytest.mark.parametrize('overrides', [
    {'mentioned_qq_ids': ['other']}, {'anonymous': True}, {'is_self': True},
    {'duplicate': True}, {'sender_principal_id': None},
    {'reply_to_platform_message_id': 'other'},
])
async def test_exclusions_never_spend_model_calls(overrides):
    attention = GroupAttention(); attention.mark_replied('room-1', 'member-1')
    provider = Classifier()
    assert (await attention.decide_smart(message('为什么', **overrides), provider=provider, model=MODEL)).action == 'observe'
    assert not provider.calls


async def test_nonparticipants_without_cues_and_closed_windows_never_classify():
    now = [1000.0]; attention = GroupAttention(clock=lambda: now[0], max_rounds=1); provider = Classifier()
    attention.mark_replied('room-1', 'member-1')
    for msg, mode in [(message('下午三点', sender_principal_id='member-2'), 'smart'),
                      (message('下午三点'), 'mention'),
                      (message('你好', anonymous=True, mentioned_qq_ids=['10002']), 'smart')]:
        assert (await attention.decide_smart(msg, trigger_mode=mode, provider=provider, model=MODEL)).action == 'observe'
    attention.mark_replied('room-1', 'member-1')
    assert (await attention.decide_smart(message('下午三点'), provider=provider, model=MODEL)).reason == 'rounds_exhausted'
    now[0] += 91
    assert (await attention.decide_smart(message('下午三点'), provider=provider, model=MODEL)).reason == 'no_active_thread'
    assert not provider.calls


@pytest.mark.parametrize('raw', ['not JSON', '```json\n{}\n```', '{"addressed":true,"confidence":0.84}',
                                   '{"addressed":false,"confidence":0.99}', '{"addressed":"true","confidence":1}',
                                   '{"addressed":true,"confidence":true}', '{"addressed":true,"confidence":NaN}',
                                   '{"addressed":true,"confidence":1,"extra":"x"}',
                                   '{"addressed":false,"addressed":true,"confidence":1}'])
async def test_classifier_requires_strict_confident_json(raw):
    attention = GroupAttention(); attention.mark_replied('room-1', 'member-1')
    assert (await attention.decide_smart(message('明天改在下午'), provider=Classifier(raw), model=MODEL)).action == 'observe'


async def test_cooldown_global_budget_day_rollover_and_restart(tmp_path):
    now = [1800000000.0]; path = tmp_path / 'attention.json'
    attention = GroupAttention(clock=lambda: now[0], daily_limit=2, state_path=path)
    provider = Classifier()
    attention.mark_replied('room-1', 'member-1')
    assert (await attention.decide_smart(message('下午三点'), provider=provider, model=MODEL)).action == 'reply'
    assert (await attention.decide_smart(message('下午四点'), provider=provider, model=MODEL)).reason == 'classifier_cooldown'
    attention = GroupAttention(clock=lambda: now[0], daily_limit=2, state_path=path)
    assert attention.thread('room-1').participants == {'member-1'}
    assert (await attention.decide_smart(message('下午四点'), provider=provider, model=MODEL)).reason == 'classifier_cooldown'
    now[0] += 60
    assert (await attention.decide_smart(message('下午五点'), provider=provider, model=MODEL)).action == 'reply'
    attention.mark_replied('room-2', 'member-1')
    assert (await attention.decide_smart(message('下午六点', room_key='room-2'), provider=provider, model=MODEL)).reason == 'classifier_budget'
    persisted = path.read_text(encoding='utf8')
    assert '下午' not in persisted
    now[0] += 86400; attention.mark_replied('room-2', 'member-1')
    assert (await attention.decide_smart(message('下午七点', room_key='room-2'), provider=provider, model=MODEL)).action == 'reply'
    assert len(provider.calls) == 3


async def test_inflight_result_cannot_rejoin_a_replaced_or_expired_thread():
    now = [1000.0]; attention = GroupAttention(clock=lambda: now[0]); attention.mark_replied('room-1', 'member-1')
    provider = Classifier(callback=lambda: (attention.forget('room-1'), attention.mark_replied('room-1', 'member-2')))
    assert (await attention.decide_smart(message('下午三点'), provider=provider, model=MODEL)).reason == 'thread_changed'
    now[0] += 60
    provider.callback = lambda: now.__setitem__(0, now[0] + 100)
    assert (await attention.decide_smart(message('为什么', sender_principal_id='member-3'), provider=provider, model=MODEL)).action == 'observe'


async def test_timeout_and_unsupported_token_limit_fail_closed(monkeypatch):
    import agi_wind.integrations.qq_attention as module
    attention = GroupAttention(); attention.mark_replied('room-1', 'member-1'); provider = Classifier()
    provider.config.supported_parameters = []
    assert (await attention.decide_smart(message('下午三点'), provider=provider, model=MODEL)).action == 'observe'
    assert not provider.calls
    provider.config.supported_parameters = ['max_tokens']
    async def timeout(awaitable, timeout):
        assert timeout == 8
        awaitable.close()
        raise TimeoutError()
    monkeypatch.setattr(module.asyncio, 'wait_for', timeout)
    assert (await attention.decide_smart(message('下午三点'), provider=provider, model=MODEL)).reason == 'classifier_unavailable'


def test_persistence_caps_rooms_and_cleans_expired_threads(tmp_path):
    now = [1000.0]; path = tmp_path / 'attention.json'
    attention = GroupAttention(clock=lambda: now[0], state_path=path)
    for index in range(105):
        attention.mark_replied(f'room-{index}', 'member-1'); now[0] += 0.01
    assert len(attention.threads) == 100
    assert len(json.loads(path.read_text(encoding='utf8'))['threads']) == 100
    now[0] += 100
    attention.mark_replied('fresh', 'member-2')
    restored = GroupAttention(clock=lambda: now[0], state_path=path)
    assert list(restored.threads) == ['fresh']


async def test_concurrent_rooms_reserve_one_global_budget_before_network():
    attention = GroupAttention(daily_limit=1)
    attention.mark_replied('room-1', 'member-1'); attention.mark_replied('room-2', 'member-1')
    started, release = asyncio.Event(), asyncio.Event()
    class Holding(Classifier):
        async def complete(self, model, messages):
            started.set(); await release.wait()
            return await super().complete(model, messages)
    provider = Holding()
    first = asyncio.create_task(attention.decide_smart(message('下午三点'), provider=provider, model=MODEL))
    await started.wait()
    second = await attention.decide_smart(message('下午三点', room_key='room-2'), provider=provider, model=MODEL)
    assert second.reason == 'classifier_budget'
    release.set(); assert (await first).action == 'reply'
    assert len(provider.calls) == 1


async def test_corrupt_state_never_resets_paid_budget(tmp_path):
    path = tmp_path / 'attention.json'; path.write_text('{corrupt', encoding='utf8')
    attention = GroupAttention(state_path=path); attention.mark_replied('room-1', 'member-1')
    provider = Classifier()
    assert (await attention.decide_smart(message('下午三点'), provider=provider, model=MODEL)).action == 'observe'
    assert not provider.calls
    assert path.read_text(encoding='utf8') == '{corrupt'


def test_atomic_replace_retries_brief_windows_sharing_denials(tmp_path, monkeypatch):
    import agi_wind.integrations.qq_attention as module
    original = module.os.replace
    attempts = []
    def transient(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise PermissionError('synthetic Windows sharing denial')
        return original(*args)
    monkeypatch.setattr(module.os, 'replace', transient)
    path = tmp_path / 'attention.json'
    attention = GroupAttention(state_path=path); attention.mark_replied('room-1', 'member-1')
    assert len(attempts) == 3
    assert json.loads(path.read_text(encoding='utf8'))['threads'][0]['room_key'] == 'room-1'

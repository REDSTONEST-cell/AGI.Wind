from types import SimpleNamespace
from uuid import uuid4

from agi_wind.core.types import ChatInput, PersonaSnapshot
from agi_wind.delivery.segments import ReplySegmenter, split_reply
from agi_wind.delivery.service import SEGMENTATION_OVERRIDE, GenerationService


def texts(value):
    return [part for part, _complete in split_reply(value)]


def test_model_line_breaks_decide_message_count():
    assert texts('你好，你来了。真的？太好了！\n我在呢') == ['你好，你来了。真的？太好了', '我在呢']
    assert texts('只想发一条就把正文写成一行。') == ['只想发一条就把正文写成一行']
    assert texts('第一行\n\n第二行') == ['第一行', '第二行']


def test_code_newlines_are_kept_and_trailing_punctuation_is_trimmed():
    assert texts('请打开 app.js。然后修改。') == ['请打开 app.js。然后修改']
    assert texts('```js\nconsole.log("hi?");\n```\n然后。') == ['```js\nconsole.log("hi?");\n```', '然后']
    assert texts('`x?y:z`可以吗？') == ['`x?y:z`可以吗']
    assert texts('版本3.14。看 https://example.com/a?q=1!x=2 。好！') == ['版本3.14。看 https://example.com/a?q=1!x=2 。好']


def test_streaming_segmenter_emits_one_message_per_finished_line():
    segmenter = ReplySegmenter()
    seen = []
    for chunk in ['你', '好。', '\n', '我在呢']:
        seen.extend(segmenter.feed(chunk))
    seen.extend(segmenter.flush())
    assert seen == ['你好', '我在呢']


def test_streaming_segmenter_flushes_unfinished_line():
    segmenter = ReplySegmenter()
    assert segmenter.feed('还没说完') == []
    assert segmenter.flush() == ['还没说完']


def test_prompt_puts_dynamic_segmentation_rule_after_persona():
    context = SimpleNamespace(
        persona_snapshot=PersonaSnapshot(
            companion_id=uuid4(),
            version=1,
            core={"name": "测试角色"},
            style={},
            relationship={},
            commitments=[],
        ),
        memories=[],
        recent_events=[],
        current_input=ChatInput(text="在吗"),
    )
    system = GenerationService(None, None).messages(context, None, None, include_sticker=False)[0]["content"]
    assert system.endswith(SEGMENTATION_OVERRIDE)
    assert "不要每轮都固定三条" in system
    assert system.rindex("【分段规则") > system.index('"persona"')


def test_prompt_tells_model_voice_is_automatic_when_enabled():
    context = SimpleNamespace(
        persona_snapshot=PersonaSnapshot(
            companion_id=uuid4(),
            version=1,
            core={"name": "测试角色"},
            style={},
            relationship={},
            commitments=[],
        ),
        memories=[],
        recent_events=[],
        current_input=ChatInput(text="发一条语音来听听"),
    )
    system = GenerationService(None, None, voice=SimpleNamespace(enabled=True)).messages(
        context, None, None, include_sticker=False
    )[0]["content"]
    assert "【语音消息】" in system
    assert "不要说自己不能发语音" in system
    assert system.index("【语音消息】") > system.index('"persona"')
    assert system.endswith(SEGMENTATION_OVERRIDE)


def test_voice_request_detection_is_narrow():
    assert GenerationService.voice_requested("发一条语音来听听")
    assert GenerationService.voice_requested("发语音的")
    assert GenerationService.voice_requested("我要听语音的")
    assert GenerationService.voice_requested("用语音说")
    assert GenerationService.voice_requested("唱一下")
    assert GenerationService.voice_requested("唱吧")
    assert GenerationService.voice_requested("想听你的声音")
    assert not GenerationService.voice_requested("语音设置在哪里")
    assert not GenerationService.voice_requested("不用发语音")
    assert not GenerationService.voice_requested("今天天气不错")


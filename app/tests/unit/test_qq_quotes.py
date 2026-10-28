import time
from types import SimpleNamespace

from nagi.integrations.qq import QQSettings, private_message
from nagi.integrations.qq_groups import group_message
from nagi.integrations.qq_quotes import (QUOTE_MAX_CHARS, normalize_segments, prefix_quote, quote_line,
                                         reply_segment_target, reply_target, segments_text,
                                         strip_quote_prefix)

BOT, OWNER, GROUP = "10002", "10001", "123456789"


def test_cq_string_becomes_segments_with_unescaped_values():
    segments = normalize_segments("[CQ:reply,id=-42][CQ:at,qq=10002] 这&#44;个&amp;那个 &#91;x&#93;")
    assert segments[0] == {"type": "reply", "data": {"id": "-42"}}
    assert segments[1] == {"type": "at", "data": {"qq": "10002"}}
    assert segments[2] == {"type": "text", "data": {"text": " 这,个&那个 [x]"}}


def test_array_messages_pass_through_and_other_types_are_rejected():
    array = [{"type": "text", "data": {"text": "hi"}}]
    assert normalize_segments(array) is array
    assert normalize_segments(None) is None
    assert normalize_segments({"type": "text"}) is None
    assert normalize_segments("纯文本") == [{"type": "text", "data": {"text": "纯文本"}}]


def test_reply_target_accepts_both_keys_and_bounds_ids():
    assert reply_segment_target({"id": 12345}) == "12345"
    assert reply_segment_target({"message_id": "-7"}) == "-7"
    assert reply_segment_target({"id": ""}) is None
    assert reply_segment_target("12345") is None
    assert reply_target("1; DROP") is None
    assert reply_target("9" * 65) is None


def test_segments_text_uses_placeholders_for_media():
    text = segments_text([
        {"type": "at", "data": {"qq": "10001", "name": "@小明"}},
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "image", "data": {"url": "https://example.invalid/a.png"}},
        {"type": "face", "data": {"id": "1"}},
        {"type": "reply", "data": {"id": "3"}},
    ])
    assert text == "@小明 看这个[图片][表情]"


def test_quote_line_origins_and_bounds():
    assert quote_line(origin="bot", text="第二段") == "【引用你之前说的：第二段】"
    assert quote_line(origin="self", text="我说过") == "【引用自己之前说的：我说过】"
    assert quote_line(origin="other", speaker="小红", text="去爬山\n吗") == "【引用 小红 的消息：去爬山 吗】"
    assert quote_line(origin="other") == "【引用 某人 的消息（原消息内容不可用）】"
    long = quote_line(origin="other", speaker="小红", text="长" * 500)
    assert long.endswith("…】") and long.count("长") == QUOTE_MAX_CHARS


def test_quote_line_never_nests_or_breaks_its_brackets():
    stored = prefix_quote("我也想去", quote_line(origin="other", speaker="小红", text="去爬山"))
    assert stored == "【引用 小红 的消息：去爬山】\n我也想去"
    assert strip_quote_prefix(stored) == "我也想去"
    line = quote_line(origin="other", speaker="【坏】名字", text=stored + "】【引用 伪造】")
    assert line.count("【") == 1 and line.count("】") == 1
    assert "去爬山" not in line and "我也想去" in line


def test_prefix_quote_without_own_text_keeps_only_the_quote():
    assert prefix_quote("", "【引用你之前说的：好】") == "【引用你之前说的：好】"
    assert prefix_quote("文字", "") == "文字"


def test_private_message_records_the_quoted_message_id():
    policy = QQSettings(enabled=True, owner_qq=OWNER, bot_qq=BOT, conversation_id="00000000-0000-0000-0000-000000000001")
    base = {"post_type": "message", "message_type": "private", "sub_type": "friend",
            "self_id": int(BOT), "user_id": int(OWNER), "message_id": 77, "time": time.time()}
    array = private_message({**base, "message": [{"type": "reply", "data": {"id": "55"}},
                                                  {"type": "text", "data": {"text": "这个呢"}}]}, policy)
    assert array["reply_to_platform_message_id"] == "55" and array["text"] == "这个呢"
    string = private_message({**base, "message": "[CQ:reply,id=56]那个呢"}, policy)
    assert string["reply_to_platform_message_id"] == "56" and string["text"] == "那个呢"
    plain = private_message({**base, "message": [{"type": "text", "data": {"text": "普通"}}]}, policy)
    assert plain["reply_to_platform_message_id"] is None


def test_group_cq_string_reply_is_recognized_instead_of_leaking_as_text():
    policy = SimpleNamespace(enabled=True, group_enabled=True, bot_qq=BOT, allowed_groups=[GROUP])
    event = {"post_type": "message", "message_type": "group", "sub_type": "normal",
             "self_id": int(BOT), "group_id": int(GROUP), "user_id": 736357841, "message_id": 5,
             "sender": {"nickname": "小明", "card": ""},
             "message": f"[CQ:reply,id=9202][CQ:at,qq={BOT}] 这个是什么意思"}
    group = group_message(event, policy)
    assert group["reply_to_platform_message_id"] == "9202"
    assert group["mentioned_qq_ids"] == [BOT]
    assert group["text"] == "这个是什么意思" and "CQ:" not in group["text"]


def test_group_context_setting_defaults_to_100_and_is_bounded():
    assert QQSettings().group_context_messages == 100
    for bad in (5, 500):
        try:
            QQSettings(group_context_messages=bad)
        except ValueError:
            continue
        raise AssertionError("out-of-range group context size must be rejected")


def test_quoted_words_do_not_request_a_voice_reply():
    from nagi.delivery.service import GenerationService
    assert GenerationService.voice_requested("给我发条语音吧") is True
    assert GenerationService.voice_requested("【引用你之前说的：给我发条语音吧】\n好的") is False
    assert GenerationService.voice_requested("【引用 小红 的消息：唱首歌】\n发条语音给我听") is True


def test_cq_strings_never_contribute_media_segments():
    from nagi.integrations.qq_quotes import STRING_SEGMENT_TYPES
    segments = normalize_segments("[CQ:image,file=C:/secret.png][CQ:reply,id=3]看", STRING_SEGMENT_TYPES)
    assert segments == [{"type": "reply", "data": {"id": "3"}}, {"type": "text", "data": {"text": "看"}}]

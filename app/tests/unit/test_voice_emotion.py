from nagi.voice.emotion import (
    DEFAULT_EMOTION,
    EMOTIONS,
    EmotionTagStream,
    instruction_for,
    normalize_emotion,
)


def test_emotion_aliases_and_unknown_fallback():
    assert normalize_emotion("高兴") == "开心"
    assert normalize_emotion("悲伤") == "难过"
    assert normalize_emotion("害怕") == "担心"
    assert normalize_emotion("愤怒") == "生气"
    assert normalize_emotion("not-an-emotion") == DEFAULT_EMOTION
    assert all(normalize_emotion(value) == value for value in EMOTIONS)


def test_stream_strips_hidden_tag_across_chunks():
    stream = EmotionTagStream()
    assert stream.feed("[情绪:") == ""
    visible = stream.feed("开心]\n今天好开心呀。")
    assert visible == "今天好开心呀。"
    assert stream.emotion == "开心"
    assert stream.feed("还有一点。") == "还有一点。"
    assert stream.flush() == ""


def test_stream_without_tag_releases_text_immediately():
    stream = EmotionTagStream()
    assert stream.feed("今天天气不错。") == "今天天气不错。"
    assert stream.emotion is None


def test_stream_flushes_incomplete_prefix_at_end():
    stream = EmotionTagStream()
    assert stream.feed("[情") == ""
    assert stream.flush() == "[情"


def test_stream_accepts_chinese_brackets_and_alias():
    stream = EmotionTagStream()
    assert stream.feed("【情绪：高兴】\n好呀。") == "好呀。"
    assert stream.emotion == "开心"


def test_instruction_contains_tone_and_emotion_direction():
    happy = instruction_for("开心", "温柔、简短")
    sad = instruction_for("难过", "温柔、简短")
    assert "温柔、简短" in happy
    assert "带笑意" in happy
    assert "低落" in sad
    assert happy != sad


def test_stream_strips_sticker_directive_after_emotion_across_chunks():
    stream = EmotionTagStream()
    assert stream.feed("[情绪:开心]\n[表情") == ""
    visible = stream.feed("包:困困摸鱼]\n给你一张。")
    assert visible == "给你一张。"
    assert stream.emotion == "开心"
    assert stream.sticker_requested is True
    assert stream.sticker_keyword == "困困摸鱼"
    assert stream.flush() == ""


def test_stream_directive_without_keyword_and_empty_library_prompt():
    stream = EmotionTagStream()
    assert stream.feed("[情绪:平静]\n[表情包]\n好呀。") == "好呀。"
    assert stream.sticker_requested is True
    assert stream.sticker_keyword == ""


def test_sticker_directive_alone_does_not_leak():
    stream = EmotionTagStream()
    assert stream.feed("[表情包:猫]") == ""
    assert stream.flush() == ""
    assert stream.sticker_requested is True
    assert stream.sticker_keyword == "猫"


def test_bare_emotion_tag_is_stripped_across_chunks():
    stream = EmotionTagStream()
    assert stream.feed("[平") == ""
    visible = stream.feed("静]\n今天没什么事。")
    assert visible == "今天没什么事。"
    assert stream.emotion == "平静"
    assert stream.feed("只是有点困。") == "只是有点困。"


def test_bare_alias_emotion_tag_and_fullwidth_brackets():
    stream = EmotionTagStream()
    assert stream.feed("【高兴】\n好呀。") == "好呀。"
    assert stream.emotion == "开心"


def test_bare_emotion_line_without_brackets_is_stripped():
    stream = EmotionTagStream()
    assert stream.feed("开心") == ""
    visible = stream.feed("\n在呢")
    assert visible == "在呢"
    assert stream.emotion == "开心"
    assert stream.feed("怎么了？") == "怎么了？"


def test_bare_emotion_line_is_stripped_in_one_chunk():
    stream = EmotionTagStream()
    assert stream.feed("平静\n今天没什么事。") == "今天没什么事。"
    assert stream.emotion == "平静"


def test_plain_emotion_prefix_is_not_swallowed_without_newline():
    stream = EmotionTagStream()
    assert stream.feed("开心呀，我在呢。") == "开心呀，我在呢。"
    assert stream.emotion is None


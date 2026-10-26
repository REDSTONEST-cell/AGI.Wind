from agi_wind.voice.text import clean_for_speech


def test_speech_text_strips_markdown_links_code_and_emoji():
    value = "**哥哥**，今天也辛苦啦！\n\n外面风有点凉。`代码` https://example.com 😊"
    spoken = clean_for_speech(value, 400)
    assert spoken.text == "哥哥，今天也辛苦啦！外面风有点凉。代码 链接。"
    assert spoken.truncated is False


def test_speech_text_removes_emotion_tags_and_fenced_blocks():
    spoken = clean_for_speech("[情绪:开心]\n```python\nprint(1)\n```\n好呀。", 400)
    assert "情绪" not in spoken.text
    assert "print" not in spoken.text
    assert "代码" in spoken.text


def test_speech_text_is_bounded_and_marks_truncation():
    spoken = clean_for_speech("一" * 450, 400)
    assert spoken.truncated is True
    assert len(spoken.text) <= 400
    assert spoken.text.endswith("。")


def test_speech_text_keeps_question_and_exclamation_boundaries():
    spoken = clean_for_speech("真的吗？！\n那太好了！", 400)
    assert spoken.text == "真的吗？！那太好了！"


def test_speech_text_empty_input_stays_empty():
    assert clean_for_speech("", 400).text == ""
    assert clean_for_speech("  \n ", 400).text == ""

from agi_wind.delivery.segments import ReplySegmenter, split_reply


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

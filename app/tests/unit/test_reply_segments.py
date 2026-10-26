from agi_wind.delivery.segments import ReplySegmenter, split_reply


def texts(value):
    return [part for part, _complete in split_reply(value)]


def test_sentence_punctuation_and_newlines_split_without_orphans():
    assert texts('你好，你来了。真的？太好了！\n我在呢') == ['你好，你来了', '真的', '太好了', '我在呢']
    assert texts('？！你认真的？!真的好耶！') == ['？！你认真的', '!真的好耶']
    assert texts('？！') == ['？！']
    assert texts('她说“好呀。”然后呢？！') == ['她说“好呀”', '然后呢']


def test_code_urls_and_decimals_keep_internal_punctuation():
    assert texts('请打开 app.js。然后修改。') == ['请打开 app.js', '然后修改']
    assert texts('1. 打开文件\n2. 保存') == ['1. 打开文件', '2. 保存']
    assert texts('版本3.14。看 https://example.com/a?q=1!x=2 。好！') == [
        '版本3.14',
        '看 https://example.com/a?q=1!x=2',
        '好',
    ]
    assert texts('`x?y:z`可以吗？') == ['`x?y:z`可以吗']
    assert texts('```js\nconsole.log("hi?");\n```\n然后。') == ['```js\nconsole.log("hi?");\n```', '然后']


def test_streaming_segmenter_emits_stable_segments_in_order():
    segmenter = ReplySegmenter()
    seen = []
    for chunk in ['你', '好。', '？你', '来啦', '！']:
        seen.extend(segmenter.feed(chunk))
    seen.extend(segmenter.flush())
    assert seen == ['你好', '？你来啦']


def test_streaming_segmenter_keeps_trailing_closing_quote():
    segmenter = ReplySegmenter()
    seen = []
    for chunk in ['她说“好呀。', '”然后', '呢？！']:
        seen.extend(segmenter.feed(chunk))
    seen.extend(segmenter.flush())
    assert seen == ['她说“好呀”', '然后呢']


def test_streaming_segmenter_flushes_single_unterminated_segment():
    segmenter = ReplySegmenter()
    assert segmenter.feed('还没说完') == []
    assert segmenter.flush() == ['还没说完']

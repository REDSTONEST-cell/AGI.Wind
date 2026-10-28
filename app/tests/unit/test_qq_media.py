import base64
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agi_wind.integrations.qq import QQBridge, QQSettings, private_message
from agi_wind.integrations.qq_groups import group_message


def test_voice_only_private_and_group_messages_are_accepted():
    settings = QQSettings(enabled=True, owner_qq='10001', bot_qq='10002',
                          conversation_id=uuid4(), group_enabled=True)
    event = dict(post_type='message', message_type='private', sub_type='friend',
                 self_id=10002, user_id=10001, message_id=-8,
                 message=[{'type': 'record', 'data': {'file': 'cached-record'}}])
    parsed = private_message(event, settings)
    assert parsed and parsed['records'] == ['cached-record']
    assert parsed['platform_message_id'] == '-8'
    event.update(message_type='group', sub_type='normal', group_id=123456)
    assert group_message(event, settings)['records'] == ['cached-record']


async def test_prepare_input_uses_real_qq_transcript_and_cached_image(tmp_path, monkeypatch):
    from agi_wind.integrations import qq
    asset_id = uuid4()
    class Assets:
        max_image_bytes = 1000
        async def put(self, actor, stream, mime):
            assert stream.read() == b'image'
            return {'id': asset_id}
    class Connection:
        async def call(self, action, params):
            if action == 'get_record_text':
                assert params == {'message_id': '-8'}
                return {'text': '这是语音内容'}
            assert action == 'get_image' and params == {'file': 'cached-image'}
            return {'url': 'https://gchat.qpic.cn/test'}
    async def download(url, limit):
        assert url == 'https://gchat.qpic.cn/test'
        return b'image', 'image/png'
    monkeypatch.setattr(qq, 'download_image', download)
    bridge = QQBridge(None, SimpleNamespace(qq=QQSettings()), None, None, Assets(), None, None)
    text, assets = await bridge._prepare_input(Connection(), {
        'text': '听听', 'records': ['record'], 'platform_message_id': '-8',
        'images': [], 'image_files': ['cached-image']})
    assert text == '听听\n[语音转文字] 这是语音内容'
    assert assets == [asset_id]


async def test_reply_sends_voice_instead_of_repeating_text(tmp_path):
    audio_id, image_id, segment_id = uuid4(), uuid4(), str(uuid4())
    path = tmp_path / 'media'; path.write_bytes(b'media')
    class Assets:
        def get(self, actor, asset_id):
            kind = 'audio' if asset_id == audio_id else 'image'
            return {'kind': kind, 'bytes': 5}, path
    bridge = QQBridge(None, SimpleNamespace(qq=QQSettings()), None, None, Assets(), None, None)
    parts = await bridge._reply_parts({'text': '你好。', 'segments': [
        {'index': 0, 'kind': 'voice', 'text': '你好', 'segment_id': segment_id}],
        'attachments': [
            {'kind': 'audio', 'asset_id': str(audio_id), 'segment_id': segment_id},
            {'kind': 'image', 'asset_id': str(image_id)}]})
    # 语音段只发 record，不再重复发文字；表情包照发
    assert [item['type'] for item in parts] == ['record', 'image']
    assert parts[0]['data']['file'] == 'base64://' + base64.b64encode(b'media').decode()
    # 混音段保持顺序：文字段发文字，语音段发语音
    mixed = await bridge._reply_parts({'text': '一。二。', 'segments': [
        {'index': 0, 'kind': 'text', 'text': '一'},
        {'index': 1, 'kind': 'voice', 'text': '二', 'segment_id': segment_id}],
        'attachments': [{'kind': 'audio', 'asset_id': str(audio_id), 'segment_id': segment_id}]})
    assert [item['type'] for item in mixed] == ['text', 'record']
    # 表情包附件（kind=sticker）作为图片发出
    sticker_parts = await bridge._reply_parts({'text': '', 'attachments': [
        {'kind': 'sticker', 'asset_id': str(image_id)}]})
    assert [item['type'] for item in sticker_parts] == ['image']
    # 语音没合成好时回退文字
    fallback = await bridge._reply_parts({'text': '文字保留。', 'segments': [
        {'index': 0, 'kind': 'voice', 'text': '文字保留', 'segment_id': str(uuid4())}]})
    assert fallback[0]['data']['text'] == '文字保留'
    assert (await bridge._reply_parts({'text': '文字保留。', 'voices': [{'state': 'failed'}]}))[0]['data']['text'] == '文字保留'


async def test_waits_only_for_current_generations_speech(monkeypatch):
    bridge = QQBridge(None, SimpleNamespace(qq=QQSettings()), None, None, None, None, None)
    sequence = iter([{'voices': [{'state': 'synthesizing'}]},
                     {'voices': [{'state': 'ready'}], 'text': '完成'}])
    bridge._assistant_content = lambda: next(sequence)
    assert (await bridge._reply_content())['text'] == '完成'


async def test_http_qq_images_upgrade_to_https_before_download(monkeypatch):
    from agi_wind.integrations import qq
    seen = []
    def dns(host, port, **kwargs):
        seen.append(host)
        raise RuntimeError('network deliberately stopped')
    monkeypatch.setattr(qq.socket, 'getaddrinfo', dns)
    with pytest.raises(RuntimeError):
        await qq.download_image('http://multimedia.nt.qq.com.cn/a', 100)
    assert seen == ['multimedia.nt.qq.com.cn']

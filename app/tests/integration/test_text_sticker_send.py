import json
from io import BytesIO

from PIL import Image

from agi_wind.core.types import ModelRef
from agi_wind.providers.protocol import ProviderChunk


class HiddenTagStickerProvider:
    """Main reply hides the sticker directive; no second reflection call is allowed."""

    def __init__(self, directive):
        self.directive = directive
        self.calls = []

    async def stream(self, model, messages):
        self.calls.append(messages)
        assert '表情包库' in messages[0]['content']
        yield ProviderChunk('reasoning', '先正常回答')
        yield ProviderChunk('text', self.directive + '\n给你一张。')


class PlainProvider:
    def __init__(self):
        self.calls = []

    async def stream(self, model, messages):
        self.calls.append(messages)
        yield ProviderChunk('text', '[情绪:平静]\n今天天气不错。')


async def seed_sticker(db, actor, seeded_scope, tmp_path, *, name='小猫', tags=('猫', '可爱')):
    from agi_wind.archive.assets import AssetStore
    from agi_wind.archive.stickers import StickerService

    store = AssetStore(db, tmp_path / 'assets')
    image = BytesIO()
    Image.new('RGB', (4, 4), 'purple').save(image, 'PNG')
    asset = await store.put(actor, BytesIO(image.getvalue()), 'image/png')
    sticker = StickerService(db).save_from_asset(
        actor, seeded_scope.companion_id, asset['id'], name=name, tags=list(tags), source='uploaded'
    )
    return asset, sticker


async def test_text_turn_sends_sticker_from_hidden_directive(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from agi_wind.delivery.service import GenerationService

    asset, sticker = await seed_sticker(db, actor, seeded_scope, tmp_path)
    provider = HiddenTagStickerProvider('[表情包:小猫]')
    service = GenerationService(db, provider, model=ModelRef(provider_id='fake', model_id='synthetic'), random_value=lambda: 0)
    request = chat_request_factory(text='发一个表情包给我', conversation_id=seeded_scope.conversation_id)
    accepted = await service.submit(actor, request)
    assert await service.run(accepted.generation_id)

    assert len(provider.calls) == 1
    events = service.replay(actor, accepted.generation_id)
    assert any(event.type == 'sticker.sent' for event in events)
    hidden = [event for event in events if event.type == 'text.segment']
    assert hidden and all('[表情包' not in event.payload['text'] for event in hidden)
    content = db.all('events', role='assistant')[0]['content']
    assert content['text'] == '给你一张。'
    assert content['attachments'][0]['asset_id'] == str(asset['id'])
    assert content['attachments'][0]['trigger'] == 'library'
    assert content['segments'][0]['text'] == '给你一张'
    assert db.all('stickers')[0]['use_count'] == 1
    # The system prompt tells the model the real library and forbids capability denial.
    assert '小猫' in provider.calls[0][0]['content']
    assert '不能发表情包' in provider.calls[0][0]['content']


async def test_text_turn_does_not_send_sticker_when_not_asked(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from agi_wind.delivery.service import GenerationService

    await seed_sticker(db, actor, seeded_scope, tmp_path)
    provider = PlainProvider()
    service = GenerationService(db, provider, model=ModelRef(provider_id='fake', model_id='synthetic'), random_value=lambda: 1)
    request = chat_request_factory(text='今天天气怎么样', conversation_id=seeded_scope.conversation_id)
    accepted = await service.submit(actor, request)
    assert await service.run(accepted.generation_id)
    assert len(provider.calls) == 1
    assert not any(event.type == 'sticker.sent' for event in service.replay(actor, accepted.generation_id))


async def test_directive_without_sticker_library_does_not_send(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from agi_wind.delivery.service import GenerationService

    provider = HiddenTagStickerProvider('[表情包:猫]')
    service = GenerationService(db, provider, model=ModelRef(provider_id='fake', model_id='synthetic'), random_value=lambda: 0)
    request = chat_request_factory(text='发一个表情包给我', conversation_id=seeded_scope.conversation_id)
    accepted = await service.submit(actor, request)
    assert await service.run(accepted.generation_id)
    content = db.all('events', role='assistant')[0]['content']
    assert content['text'] == '给你一张。'
    assert not any(item['kind'] == 'sticker' for item in content.get('attachments', []))
    # The hidden directive still never leaks into visible text or SSE.
    text = ''.join(event.payload['text'] for event in service.replay(actor, accepted.generation_id) if event.type == 'text.delta')
    assert '[表情包' not in text
    assert '表情包库为空' in provider.calls[0][0]['content']

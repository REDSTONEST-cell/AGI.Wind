import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from test_idle_cognition import make_service, active_settings, cognition, run_turn
from nagi.integrations.qq import QQSettings


async def test_qq_source_targets_qq_not_an_unrelated_channel(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition(script=[json.dumps({'action': 'reach_out', 'message': '刚想起一件事'})])
    settings = active_settings()
    settings.qq = QQSettings(enabled=True, owner_qq='10001', bot_qq='10002', conversation_id=seeded_scope.conversation_id)
    service, proactive, idle = make_service(db, provider, settings, clock)
    sent = []
    class Bridge:
        def proactive_available(self, target): return target['kind'] == 'private' and target['recipient'] == '10001'
        async def deliver_proactive(self, target, content, result, check):
            await check(); sent.append((target, content))
    service.qq_bridge = Bridge()
    request = chat_request_factory(text='你好', conversation_id=seeded_scope.conversation_id)
    request.device_id = 'qq-private:10002:10001'
    accepted = await service.submit(actor, request)
    await service.run(accepted.generation_id)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert sent and sent[0][0]['kind'] == 'private'
    assert db.all('idle_cognition_runs')[0]['state'] == 'sent'


async def test_qq_binding_changed_before_idle_makes_no_model_call(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition()
    settings = active_settings()
    settings.qq = QQSettings(enabled=True, owner_qq='10001', bot_qq='10002', conversation_id=seeded_scope.conversation_id)
    service, _, idle = make_service(db, provider, settings, clock)
    service.qq_bridge = SimpleNamespace(proactive_available=lambda target: True)
    request = chat_request_factory(text='你好', conversation_id=seeded_scope.conversation_id)
    request.device_id = 'qq-private:10002:10001'
    accepted = await service.submit(actor, request)
    await service.run(accepted.generation_id)
    settings.qq = settings.qq.model_copy(update={'owner_qq': '111111111'})
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert provider.complete_calls == 0
    assert db.all('idle_cognition_runs')[0]['state'] == 'skipped'


async def test_group_idle_uses_group_scope_and_same_target(db, actor, seeded_scope, monkeypatch):
    from nagi.integrations.qq_groups import ensure_group_binding
    from nagi.memory.context import ContextBuilder
    from nagi.archive import models as m
    from nagi.core.types import SceneScope
    from uuid import uuid4
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition(script=[json.dumps({'action': 'reach_out', 'message': '补充一下'})])
    settings = active_settings()
    settings.qq = QQSettings(enabled=True, owner_qq='10001', bot_qq='10002',
        conversation_id=seeded_scope.conversation_id, group_enabled=True, allowed_groups=['123456789'])
    service, _, idle = make_service(db, provider, settings, clock)
    sent=[]
    async def deliver(target, content, result, check):
        await check(); sent.append(target); return {'sent': True}
    service.qq_bridge = SimpleNamespace(proactive_available=lambda target: True, deliver_proactive=deliver)
    binding=ensure_group_binding(db, actor, companion_id=seeded_scope.companion_id, bot_qq='10002', group_id='123456789')
    event=service.archive.record_external_message(actor, binding['conversation_id'], text='你好', external_message_id='synthetic-idle')
    accepted=service.archive.submit_external_reply(actor,binding['conversation_id'],event['id'],uuid4(),
        output={},device_id='qq-group:10002:123456789')
    await service.run(accepted.generation_id)
    scopes=[]
    original=ContextBuilder.build
    async def build(self, scope, *args, **kwargs):
        scopes.append(scope); return await original(self, scope, *args, **kwargs)
    monkeypatch.setattr(ContextBuilder,'build',build)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert sent and sent[0]['recipient'] == '123456789'
    assert len(scopes) == 1 and isinstance(scopes[0], SceneScope)
    assert scopes[0].scene == 'group' and scopes[0].room_id == binding['id']


async def test_proactive_media_is_referenced_in_archive(db, actor, seeded_scope, tmp_path):
    from io import BytesIO
    from PIL import Image
    from nagi.archive.assets import AssetStore
    from nagi.archive.service import ArchiveService
    image=BytesIO()
    Image.new('RGB',(4,4),'blue').save(image,format='PNG'); image.seek(0)
    asset=await AssetStore(db,tmp_path/'media').put(actor,image,'image/png')
    result=ArchiveService(db).create_proactive_message(actor,seeded_scope.conversation_id,'给你看看',
        media={'attachments':[{'asset_id':str(asset['id']),'kind':'sticker'}]})
    assert any(row['event_id']==result['event_id'] and row['asset_id']==asset['id'] for row in db.all('asset_refs'))
    output=next(row for row in db.all('events') if row['id']==result['event_id'])
    assert output['content']['attachments'][0]['kind']=='sticker'

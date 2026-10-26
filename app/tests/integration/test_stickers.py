from io import BytesIO
from uuid import uuid4
import json
import zipfile
import pytest
from PIL import Image
from agi_wind.core.errors import AppError


async def image_asset(db, actor, root):
    from agi_wind.archive.assets import AssetStore
    stream = BytesIO()
    Image.new('RGB', (4, 4), 'red').save(stream, 'PNG')
    store = AssetStore(db, root)
    return store, await store.put(actor, BytesIO(stream.getvalue()), 'image/png')


async def test_sticker_scope_dedup_manual_name_and_archive(db, actor, seeded_scope, tmp_path):
    from agi_wind.archive.stickers import StickerService
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    from agi_wind.core.types import Actor
    store, asset = await image_asset(db, actor, tmp_path/'assets')
    stickers = StickerService(db)
    first = stickers.save_from_asset(actor, seeded_scope.companion_id, asset['id'], name='红猫')
    stickers.update(actor, first['id'], {'name': '我的名字', 'state': 'hidden'})
    again = stickers.save_from_asset(actor, seeded_scope.companion_id, asset['id'], name='模型名字', source='captured')
    assert again['id'] == first['id'] and again['name'] == '我的名字'
    assert again['state'] == 'hidden'
    with pytest.raises(AppError):
        stickers.update(Actor(account_id=uuid4(), user_id=uuid4()), first['id'], {'name':'越权'})
    package = await ExportService(db, store.root).export(actor, seeded_scope.companion_id, tmp_path/'copy.zip')
    report = ImportService.validate(package)
    assert report['manifest']['archive_format_version'] == 3
    assert report['records']['stickers'] == 1
    restored = await ImportService(db, tmp_path/'restored').restore(package, mode='clone')
    assert len(stickers.list(actor, restored['companion_id'], allow_quarantine=True)) == 1


async def test_old_v2_archive_without_stickers_is_importable(db, actor, seeded_scope, tmp_path):
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    package = await ExportService(db, tmp_path/'assets').export(actor, seeded_scope.companion_id, tmp_path/'new.zip')
    with zipfile.ZipFile(package) as z:
        files = {name:z.read(name) for name in z.namelist()}
    manifest = json.loads(files.pop('manifest.json'))
    manifest.update(archive_format_version=2, schema_version='0007_conversation_lifecycle')
    manifest['files'].pop('stickers.ndjson', None)
    files.pop('stickers.ndjson', None)
    old = tmp_path/'old.zip'
    with zipfile.ZipFile(old, 'w') as z:
        for name, raw in files.items(): z.writestr(name, raw)
        z.writestr('manifest.json', json.dumps(manifest))
    assert ImportService.validate(old)['valid']
    await ImportService(db, tmp_path/'restored').restore(old, mode='clone')


async def test_deleting_source_removes_captured_sticker_but_preserves_manual_shared_asset(db, actor, seeded_scope, archive, chat_request_factory, tmp_path):
    from agi_wind.archive.stickers import StickerService
    from agi_wind.archive.deletions import DeletionService, DeletionLedger
    store, asset = await image_asset(db, actor, tmp_path/'assets')
    request = chat_request_factory(conversation_id=seeded_scope.conversation_id)
    request.input.asset_ids = [asset['id']]
    await archive.accept(actor, request)
    event = db.all('events', role='user')[0]
    service = StickerService(db)
    sticker = service.save_from_asset(actor, seeded_scope.companion_id, asset['id'], name='手动保留')
    service.save_from_asset(actor, seeded_scope.companion_id, asset['id'], source_event_id=event['id'], source='captured', name='自动')
    ledger = DeletionLedger(tmp_path/'governance'); ledger.initialize()
    await DeletionService(db, ledger, store.root).delete_event(actor, event['id'])
    assert db.count('stickers') == 1
    assert db.all('stickers')[0]['id'] == sticker['id']
    assert store.get(actor, asset['id'])[1].exists()

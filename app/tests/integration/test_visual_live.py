"""Opt-in vendor check using only synthetic data in a disposable PostgreSQL schema."""
import os
from io import BytesIO
from pathlib import Path
import pytest
from PIL import Image,ImageDraw

pytestmark=[pytest.mark.live,pytest.mark.skipif(os.environ.get('AGI_RUN_VISUAL_LIVE')!='1',reason='explicit opt-in vendor call')]


async def test_real_vendor_image_reply_and_reflection(db,actor,seeded_scope,chat_request_factory,tmp_path,monkeypatch):
    from nagi.api.app import from_env
    from nagi.archive.assets import AssetStore
    from nagi.providers.vision import VisionAdapter
    from nagi.delivery.service import GenerationService
    root=Path(__file__).resolve().parents[3]
    monkeypatch.setenv('AGI_CONFIG',str(root/'config/app.local.yaml'))
    source_app=from_env(); real=source_app.state.service
    assert real.provider and real.vision,'configured dialogue/vision required'
    store=AssetStore(db,tmp_path/'assets')
    image=Image.new('RGB',(128,96),'white');draw=ImageDraw.Draw(image);draw.rectangle((16,16,112,80),fill='red')
    data=BytesIO();image.save(data,'PNG')
    asset=await store.put(actor,BytesIO(data.getvalue()),'image/png')
    service=GenerationService(db,real.provider,model=real.model,vision=VisionAdapter(db,store,real.vision.complete,real.vision.model),random_value=lambda:1)
    request=chat_request_factory(text='请用一句中文描述这张测试图的颜色和形状。',conversation_id=seeded_scope.conversation_id);request.input.asset_ids=[asset['id']]
    try:
        accepted=await service.submit(actor,request)
        assert await service.run(accepted.generation_id)
        output=db.all('events',role='assistant')[0]['content']
        assert output['text'] and output['state']=='complete'
        tools=[s for s in output['trace'] if s['type']=='tool']
        assert all(s['status']=='completed' for s in tools if s['tool'] in ('observe_image','reply','reflect'))
        print('LIVE: real vision, reply, reflection completed; reasoning chars='+str(len(output.get('reasoning','')))+'; tools='+','.join(s['tool']+':'+s['status'] for s in tools))
    finally:
        await real.provider.aclose()
        vision_provider=getattr(real.vision.complete,'__self__',None)
        if vision_provider and vision_provider is not real.provider: await vision_provider.aclose()
        await real.registry.aclose();source_app.state.db.engine.dispose()

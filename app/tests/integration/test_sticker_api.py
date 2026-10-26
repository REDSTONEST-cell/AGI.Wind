from io import BytesIO
from uuid import uuid4
import httpx
from PIL import Image
from agi_wind.core.settings import Settings


async def test_gif_upload_sticker_crud_and_image_only_history(db, actor, seeded_scope, tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.core.types import ChatRequest
    app=create_app(Settings(access_token='test-token',data_dir=tmp_path),db,actor)
    data=BytesIO(); Image.new('RGB',(3,4),'blue').save(data,'GIF',save_all=True,append_images=[Image.new('RGB',(3,4),'red')],duration=100,loop=0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        headers={'Authorization':'Bearer test-token'}
        assert (await client.get('/api/v1/stickers',params={'companion_id':str(seeded_scope.companion_id)})).status_code==401
        upload=await client.post('/api/v1/assets',headers=headers,files={'file':('蓝猫.gif',data.getvalue(),'image/gif')})
        assert upload.status_code==201,upload.text
        asset=upload.json(); assert asset['metadata']['frames']==2 and asset['metadata']['original_filename']=='蓝猫.gif'
        response=await client.post('/api/v1/stickers',headers=headers,json={'companion_id':str(seeded_scope.companion_id),'asset_id':asset['id']})
        assert response.status_code==201,response.text
        sticker=response.json(); assert sticker['name']=='蓝猫.gif'
        changed=await client.patch('/api/v1/stickers/'+sticker['id'],headers=headers,json={'name':'猫','tags':['开心'],'state':'hidden'})
        assert changed.status_code==200
        assert (await client.patch('/api/v1/stickers/'+sticker['id'],headers=headers,json={'state':'wrong'})).status_code==422
        assert (await client.get('/api/v1/stickers',headers=headers,params={'companion_id':str(uuid4())})).status_code in (403,404)
        accepted=await app.state.service.submit(actor,ChatRequest(request_id=uuid4(),conversation_id=seeded_scope.conversation_id,input={'text':'','asset_ids':[asset['id']]},device_id='test'))
        history=(await client.get(f'/api/v1/conversations/{seeded_scope.conversation_id}/events',headers=headers)).json()
        assert history[0]['attachments'][0]['asset_id']==asset['id']
        assert (await client.get('/api/v1/assets/'+asset['id'],headers=headers)).content==data.getvalue()
        assert (await client.delete('/api/v1/stickers/'+sticker['id'],headers=headers)).status_code==200
        assert db.count('assets')==1


async def test_history_preserves_request_image_order(db, actor, seeded_scope, tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.core.types import ChatRequest
    app=create_app(Settings(access_token='token',data_dir=tmp_path),db,actor)
    ids=[]
    for color in ['red','blue']:
        image=BytesIO();Image.new('RGB',(2,2),color).save(image,'PNG')
        asset=await app.state.assets.put(actor,BytesIO(image.getvalue()),'image/png');ids.append(str(asset['id']))
    await app.state.service.submit(actor,ChatRequest(request_id=uuid4(),conversation_id=seeded_scope.conversation_id,input={'text':'','asset_ids':list(reversed(ids))},device_id='test'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        rows=(await client.get(f'/api/v1/conversations/{seeded_scope.conversation_id}/events',headers={'Authorization':'Bearer token'})).json()
    assert [item['asset_id'] for item in rows[0]['attachments']]==list(reversed(ids))


class FakeVision:
    def __init__(self):
        from agi_wind.core.types import ModelRef
        self.model=ModelRef(provider_id='fake',model_id='vision')
        self.calls=[]

    async def complete(self,model,messages):
        self.calls.append(messages)
        return '{"name":"蓝猫","tags":["猫","蓝色"]}'


async def test_manual_sticker_upload_gets_auto_name_and_tags(db, actor, seeded_scope, tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.core.types import ModelRef
    from agi_wind.delivery.service import GenerationService
    service=GenerationService(db,None,model=ModelRef(provider_id='fake',model_id='text'))
    service.vision=FakeVision()
    app=create_app(Settings(access_token='token',data_dir=tmp_path),db,actor,generation_service=service)
    data=BytesIO();Image.new('RGB',(5,6),'blue').save(data,'PNG')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        headers={'Authorization':'Bearer token'}
        upload=await client.post('/api/v1/assets',headers=headers,files={'file':('d9aa05c6-8567.png',data.getvalue(),'image/png')})
        asset=upload.json()
        response=await client.post('/api/v1/stickers',headers=headers,json={'companion_id':str(seeded_scope.companion_id),'asset_id':asset['id']})
        sticker=response.json()
        assert sticker['name']=='蓝猫' and sticker['tags']==['猫','蓝色']
        await client.patch('/api/v1/stickers/'+sticker['id'],headers=headers,json={'tags':[]})
        tagged=(await client.post('/api/v1/stickers/'+sticker['id']+'/autotag',headers=headers)).json()
        assert tagged['tags']==['猫','蓝色']
    assert len(service.vision.calls)==2


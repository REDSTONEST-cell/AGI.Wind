import asyncio
from uuid import uuid4
import httpx
import pytest
from nagi.core.settings import Settings
from nagi.core.types import ModelRef

def client_app(db,actor,tmp_path,provider=None):
    from nagi.api.app import create_app
    from nagi.delivery.service import GenerationService
    settings=Settings(access_token="synthetic-private-token",data_dir=tmp_path)
    service=GenerationService(db,provider,ModelRef(provider_id="fake",model_id="test") if provider else None)
    return create_app(settings,db,actor,service)

async def connect(client):
    issued=await client.post('/api/v1/client/launch',headers={'Authorization':'Bearer synthetic-private-token'})
    assert issued.status_code==200
    ticket=issued.json()['ticket']
    response=await client.post('/api/v1/client/session',headers={'Origin':'http://test'},json={'ticket':ticket})
    assert response.status_code==200
    assert 'synthetic-private-token' not in response.text
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'samesite=strict' in response.headers['set-cookie'].lower()
    return ticket,response.json()['csrf_token']

async def test_client_cookie_login_replay_csrf_and_logout(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/v1/client/session')).status_code==401
        ticket,csrf=await connect(client)
        assert (await client.get('/api/v1/companions')).status_code==200
        assert (await client.get('/api/v1/client/session')).json()['csrf_token']==csrf
        replay=await client.post('/api/v1/client/session',headers={'Origin':'http://test'},json={'ticket':ticket})
        assert replay.status_code==401
        body={'companion_id':str(seeded_scope.companion_id),'title':'合成客户端会话'}
        assert (await client.post('/api/v1/conversations',json=body)).status_code==403
        evil=await client.post('/api/v1/conversations',json=body,headers={'Origin':'https://untrusted.invalid','X-AGI-CSRF':csrf})
        assert evil.status_code==403
        good=await client.post('/api/v1/conversations',json=body,headers={'Origin':'http://test','X-AGI-CSRF':csrf})
        assert good.status_code==201
        listed=await client.get('/api/v1/conversations',params={'companion_id':str(seeded_scope.companion_id)})
        assert any(row['id']==good.json()['conversation_id'] for row in listed.json())
        logout=await client.delete('/api/v1/client/session',headers={'Origin':'http://test','X-AGI-CSRF':csrf})
        assert logout.status_code==200
        assert (await client.get('/api/v1/companions')).status_code==401

async def test_ticket_wrong_origin_does_not_log_in_or_consume_ticket(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        result=await client.post('/api/v1/client/launch',headers={'Authorization':'Bearer synthetic-private-token'})
        ticket=result.json()['ticket']
        bad=await client.post('/api/v1/client/session',headers={'Origin':'https://untrusted.invalid'},json={'ticket':ticket})
        assert bad.status_code==403 and 'set-cookie' not in bad.headers
        good=await client.post('/api/v1/client/session',headers={'Origin':'http://test'},json={'ticket':ticket})
        assert good.status_code==200

async def test_client_sources_are_authorized_and_static_does_not_leak_token(db,actor,seeded_scope,archive,chat_request_factory,tmp_path):
    app=client_app(db,actor,tmp_path)
    await archive.accept(actor,chat_request_factory(text='合成来源文本',conversation_id=seeded_scope.conversation_id))
    event=db.all('events')[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/v1/events/'+str(event['id']))).status_code==401
        await connect(client)
        assert (await client.get('/api/v1/events/'+str(event['id']))).json()['content']['text']=='合成来源文本'
        for path in ['/','/status','/client/app.js','/client/styles.css','/client/sdk/api.js']:
            result=await client.get(path)
            assert result.status_code==200,path
            assert 'synthetic-private-token' not in result.text
        assert (await client.get('/')).headers['content-type'].startswith('text/html')

async def test_reference_client_cookie_can_submit_and_receive_stream(db,actor,seeded_scope,tmp_path):
    from test_generation import FakeProvider
    app=client_app(db,actor,tmp_path,FakeProvider())
    from nagi.archive.deletions import DeletionLedger
    DeletionLedger(tmp_path/'governance').initialize()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            _,csrf=await connect(client)
            headers={'Origin':'http://test','X-AGI-CSRF':csrf}
            request={'request_id':str(uuid4()),'conversation_id':str(seeded_scope.conversation_id),'input':{'text':'合成客户端端到端测试'},'device_id':'reference-test'}
            accepted=await client.post('/api/v1/chat/requests',json=request,headers=headers)
            assert accepted.status_code==202
            gid=accepted.json()['generation_id']
            for _ in range(100):
                status=(await client.get('/api/v1/generations/'+gid)).json()
                if status['state'] in ('completed','failed'):break
                await asyncio.sleep(.02)
            assert status['state']=='completed'
            stream=await client.get(accepted.json()['events_url'])
            assert 'event: text.delta' in stream.text and 'event: generation.completed' in stream.text
            history=(await client.get(f'/api/v1/conversations/{seeded_scope.conversation_id}/events')).json()
            assert [row['role'] for row in history]==['user','assistant']

async def test_session_expires_and_launch_requires_bearer(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/api/v1/client/launch')).status_code==401
        await connect(client)
        # Expire through injected clock, not a sleep or fake authentication response.
        app.state.browser_sessions.clock=lambda:10**15
        assert (await client.get('/api/v1/companions')).status_code==401


async def test_conversation_rename_requires_csrf_and_updates_title(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.patch('/api/v1/conversations/'+str(seeded_scope.conversation_id),json={'title':'x'})).status_code==401
        _,csrf=await connect(client)
        headers={'Origin':'http://test','X-AGI-CSRF':csrf}
        assert (await client.patch('/api/v1/conversations/'+str(seeded_scope.conversation_id),json={'title':'x'})).status_code==403
        renamed=await client.patch('/api/v1/conversations/'+str(seeded_scope.conversation_id),json={'title':'和海边的下午'},headers=headers)
        assert renamed.status_code==200 and renamed.json()['title']=='和海边的下午'
        listed=(await client.get('/api/v1/conversations',params={'companion_id':str(seeded_scope.companion_id)})).json()
        assert any(row['id']==str(seeded_scope.conversation_id) and row['title']=='和海边的下午' for row in listed)


async def test_loopback_bootstrap_creates_session_without_token(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    transport=httpx.ASGITransport(app=app,client=("127.0.0.1",50000))
    async with httpx.AsyncClient(transport=transport,base_url="http://test") as client:
        result=await client.post('/api/v1/client/bootstrap',headers={'Host':'127.0.0.1:18440'})
        assert result.status_code==200,result.text
        assert 'httponly' in result.headers['set-cookie'].lower()
        assert result.json()['csrf_token']
        # The new cookie immediately authorizes normal API calls.
        assert (await client.get('/api/v1/companions')).status_code==200
        again=await client.post('/api/v1/client/bootstrap',headers={'Host':'127.0.0.1:18440'})
        assert again.status_code==200


async def test_non_loopback_bootstrap_still_requires_token(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    transport=httpx.ASGITransport(app=app,client=("203.0.113.9",51000))
    async with httpx.AsyncClient(transport=transport,base_url="http://test") as client:
        result=await client.post('/api/v1/client/bootstrap',headers={'Host':'127.0.0.1:18440'})
        assert result.status_code==401 and 'set-cookie' not in result.headers

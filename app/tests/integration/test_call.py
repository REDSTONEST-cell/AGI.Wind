import httpx
import pytest
from test_client import client_app, connect


@pytest.mark.parametrize('realtime,expected', [(True,0.05),(False,0.25)])
async def test_call_events_are_polled_faster_without_changing_normal_chat(db,actor,seeded_scope,tmp_path,chat_request_factory,monkeypatch,realtime,expected):
    import asyncio
    import agi_wind.api.app as api_module
    from agi_wind.archive import models as models
    app=client_app(db,actor,tmp_path)
    accepted=await app.state.service.submit(actor,chat_request_factory(conversation_id=seeded_scope.conversation_id,output={'voice':'off','realtime':realtime}))
    delays=[]
    original=asyncio.sleep
    async def record_sleep(delay):
        if delay in (0.05,0.25):
            delays.append(delay)
            with db.engine.begin() as conn:
                conn.execute(models.generations.update().where(models.generations.c.id==accepted.generation_id).values(state='completed'))
        await original(0)
    monkeypatch.setattr(api_module.asyncio,'sleep',record_sleep)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get(accepted.events_url,headers={'Authorization':'Bearer synthetic-private-token'})
    assert response.status_code==200
    assert delays==[expected]


async def test_call_ticket_requires_auth_csrf_and_owned_conversation(db, actor, seeded_scope, tmp_path):
    from uuid import uuid4
    app = client_app(db, actor, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        body = {'conversation_id': str(seeded_scope.conversation_id)}
        assert (await client.post('/api/v1/call/ticket', json=body)).status_code == 401
        _, csrf = await connect(client)
        assert (await client.post('/api/v1/call/ticket', json=body)).status_code == 403
        headers = {'Origin':'http://test', 'X-AGI-CSRF':csrf}
        assert (await client.post('/api/v1/call/ticket', json={'conversation_id':str(uuid4())}, headers=headers)).status_code in (403,404)
        response = await client.post('/api/v1/call/ticket', json=body, headers=headers)
        assert response.status_code == 200
        assert len(response.json()['ticket']) >= 32
        assert response.json()['expires_in'] == 30


def test_call_websocket_ticket_origin_pcm_and_disconnect_cleanup(db, actor, seeded_scope, tmp_path, monkeypatch):
    import asyncio
    import json
    import struct
    import pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    import websockets.asyncio.client

    class Upstream:
        def __init__(self): self.messages = []; self.closed = False
        async def recv(self): return json.dumps({'type':'ready','sample_rate':16000})
        async def send(self, data): self.messages.append(data)
        def __aiter__(self): return self
        async def __anext__(self): await asyncio.Future()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed = True

    upstreams = []
    def connect_upstream(*args, **kwargs):
        upstream = Upstream(); upstreams.append(upstream); return upstream
    monkeypatch.setattr(websockets.asyncio.client, 'connect', connect_upstream)
    (tmp_path/'asr-token.txt').write_text('synthetic-sidecar-only', encoding='utf-8')
    from agi_wind.archive.deletions import DeletionLedger
    DeletionLedger(tmp_path/'governance').initialize()
    app = client_app(db, actor, tmp_path)
    with TestClient(app) as client:
        def issue():
            return client.post('/api/v1/call/ticket', json={'conversation_id':str(seeded_scope.conversation_id)},
                               headers={'Authorization':'Bearer synthetic-private-token'}).json()['ticket']
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/call/stream', headers={'Origin':'https://untrusted.invalid'}): pass
        ticket = issue()
        with client.websocket_connect('/api/v1/call/stream') as ws:
            ws.send_json({'ticket':ticket})
            assert ws.receive_json()['type'] == 'ready'
            ws.send_bytes(struct.pack('<ff', 0.2, -0.1))
            ws.send_json({'type':'cancel','epoch':1})
            ws.send_json({'type':'ping'})
            ws.send_bytes(b'x')
            assert ws.receive_json()['type'] == 'speech_reset'
            assert ws.receive_json()['code'] == 'CALL_UNAVAILABLE'
        assert upstreams[0].closed
        assert isinstance(upstreams[0].messages[0], bytes)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/call/stream') as ws:
                ws.send_json({'ticket':ticket})
                ws.receive_json()
        # A new ticket can reconnect after the previous socket closes.
        with client.websocket_connect('/api/v1/call/stream') as ws:
            ws.send_json({'ticket':issue()})
            assert ws.receive_json()['type'] == 'ready'
        assert upstreams[-1].closed


async def test_realtime_chat_uses_incremental_provider_even_when_agent_is_enabled(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.agent.settings import AgentSettings
    from agi_wind.delivery.service import GenerationService
    from agi_wind.core.types import ModelRef
    class StreamingOnly:
        async def tool_turn(self, *args, **kwargs):
            raise AssertionError('Call must not wait for a non-streaming tool-planning turn')
        async def stream(self, *args):
            yield '哥哥，'
            yield '我听到了。'
    service = GenerationService(db, StreamingOnly(), model=ModelRef(provider_id='fake',model_id='test'))
    service.agent_settings = lambda: AgentSettings(enabled=True)
    service.memory_extraction_enabled = False
    request = chat_request_factory(conversation_id=seeded_scope.conversation_id, output={'voice':'off', 'realtime':True})
    accepted = await service.submit(actor, request)
    assert await service.run(accepted.generation_id)
    chunks = [event.payload['text'] for event in service.replay(actor, accepted.generation_id) if event.type == 'text.delta']
    assert chunks == ['哥哥，', '我听到了。']


def test_socket_close_cancels_only_its_call_and_blocks_late_submissions(db, actor, seeded_scope, tmp_path, monkeypatch):
    import asyncio
    import json
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from agi_wind.archive.deletions import DeletionLedger
    import websockets.asyncio.client
    class Upstream:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def recv(self): return json.dumps({'type':'ready'})
        async def send(self,data): pass
        def __aiter__(self): return self
        async def __anext__(self): await asyncio.Future()
    monkeypatch.setattr(websockets.asyncio.client, 'connect', lambda *args,**kwargs: Upstream())
    (tmp_path/'asr-token.txt').write_text('synthetic',encoding='utf8')
    DeletionLedger(tmp_path/'governance').initialize()
    app = client_app(db,actor,tmp_path)
    async def keep_pending(generation_id): return False
    app.state.service.run = keep_pending
    headers={'Authorization':'Bearer synthetic-private-token'}
    with TestClient(app) as client:
        issued=client.post('/api/v1/call/ticket',json={'conversation_id':str(seeded_scope.conversation_id)},headers=headers).json()
        def body(realtime=True):
            return {'request_id':str(uuid4()),'conversation_id':str(seeded_scope.conversation_id),
                    'input':{'text':'synthetic call'},'output':{'voice':'off','realtime':realtime},'device_id':issued['call_id']}
        assert client.post('/api/v1/chat/requests',json=body(),headers=headers).status_code==409
        with client.websocket_connect('/api/v1/call/stream') as ws:
            ws.send_json({'ticket':issued['ticket']});assert ws.receive_json()['type']=='ready'
            call=client.post('/api/v1/chat/requests',json=body(),headers=headers)
            assert call.status_code==202
            normal=client.post('/api/v1/chat/requests',json=body(False),headers=headers)
            assert normal.status_code==202
        states={str(row['id']):row['state'] for row in db.all('generations')}
        assert states[call.json()['generation_id']]=='cancelled'
        assert states[normal.json()['generation_id']]=='pending'
        assert client.post('/api/v1/chat/requests',json=body(),headers=headers).status_code==409
        normal_request=next(row for row in db.all('requests') if str(row['generation_id'])==normal.json()['generation_id'])
        assert 'realtime' not in normal_request['output']

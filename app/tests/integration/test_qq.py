import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest
from test_client import client_app


async def test_qq_settings_auth_validation_masking_and_persistence(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    headers={'Authorization':'Bearer synthetic-private-token'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        assert (await c.get('/api/v1/integrations/qq')).status_code==401
        assert (await c.get('/api/v1/integrations/qq',headers=headers)).json()['enabled'] is False
        payload=dict(enabled=True,owner_qq='3548782171',bot_qq='334193096',
                     conversation_id=str(seeded_scope.conversation_id),token='synthetic-qq-token-32-characters-long')
        response=await c.put('/api/v1/integrations/qq',headers=headers,json=payload)
        assert response.status_code==200
        assert response.json()['token_configured'] is True
        assert payload['token'] not in response.text
        assert payload['token'] not in (tmp_path/'settings.json').read_text()
        from agi_wind.core.settings import Settings,apply_persisted_settings
        restored=apply_persisted_settings(Settings(data_dir=tmp_path))
        assert restored.qq.owner_qq=='3548782171'
        assert restored.qq.conversation_id==seeded_scope.conversation_id


def test_qq_ws_rejects_disabled_and_wrong_credentials(db,actor,tmp_path):
    app=client_app(db,actor,tmp_path)
    from agi_wind.archive.deletions import DeletionLedger
    DeletionLedger(tmp_path/'governance').initialize()
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/api/v1/integrations/qq/ws',headers={'X-Self-ID':'334193096'}): pass


def test_qq_real_websocket_roundtrip_uses_archive_segments_and_deduplicates(db,actor,seeded_scope,tmp_path):
    from agi_wind.archive.deletions import DeletionLedger
    from agi_wind.integrations.qq import QQSettings,TOKEN_REF
    from agi_wind.providers.protocol import ProviderChunk
    class Provider:
        calls=0
        async def stream(self,*args):
            self.calls+=1
            yield ProviderChunk(kind='reasoning',text='synthetic-private-reasoning')
            yield '你好。\n今天怎么样？'
    provider=Provider()
    app=client_app(db,actor,tmp_path,provider)
    app.state.service.memory_extraction_enabled=False
    app.state.settings.memory_extraction_enabled=False
    app.state.settings.memory_maintenance_enabled=False
    app.state.settings.qq=QQSettings(enabled=True,owner_qq='3548782171',bot_qq='334193096',
        conversation_id=seeded_scope.conversation_id,reply_delay_seconds=0)
    token='synthetic-qq-token-32-characters-long'
    app.state.settings_store.set(TOKEN_REF,token)
    DeletionLedger(tmp_path/'governance').initialize()
    event=dict(post_type='message',message_type='private',sub_type='friend',
        self_id=334193096,user_id=3548782171,message_id=10,
        message=[{'type':'text','data':{'text':'synthetic hello'}}])
    with TestClient(app) as c:
        for headers in ({'X-Self-ID':'334193096'},
                        {'X-Self-ID':'99999','Authorization':'Bearer '+token}):
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect('/api/v1/integrations/qq/ws',headers=headers): pass
        with c.websocket_connect('/api/v1/integrations/qq/ws',headers={
                'X-Self-ID':'334193096','Authorization':'Bearer '+token}) as ws:
            ws.send_json(event); ws.send_json(event)
            sent=[]
            for _ in range(2):
                action=ws.receive_json(); sent.append(action)
                assert action['action']=='send_private_msg'
                ws.send_json({'status':'ok','retcode':0,'data':{'message_id':100},'echo':action['echo']})
            assert [a['params']['message'][0]['data']['text'] for a in sent]==['你好','今天怎么样']
            assert all(a['params']['user_id']==3548782171 for a in sent)
            assert 'reasoning' not in str(sent)
            assert provider.calls==1 and db.count('requests')==1


async def test_qq_concurrent_connections_cannot_both_be_admitted(db,actor,seeded_scope,tmp_path):
    import asyncio
    from agi_wind.integrations.qq import QQSettings,TOKEN_REF,WS_PATH
    app=client_app(db,actor,tmp_path)
    app.state.settings.qq=QQSettings(enabled=True,owner_qq='3548782171',bot_qq='334193096',conversation_id=seeded_scope.conversation_id)
    token='synthetic-qq-token-32-characters-long'; app.state.settings_store.set(TOKEN_REF,token)
    endpoint=next(route.endpoint for route in app.routes if getattr(route,'path',None)==WS_PATH)
    entered,accept_release,receive_release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    class Wire:
        accepted=False
        closed=None
        headers={'authorization':'Bearer '+token,'x-self-id':'334193096'}
        async def accept(self):
            self.accepted=True; entered.set(); await accept_release.wait()
        async def receive_text(self):
            await receive_release.wait(); raise WebSocketDisconnect()
        async def close(self,code=1000): self.closed=code
    first,second=Wire(),Wire()
    a=asyncio.create_task(endpoint(first)); await entered.wait()
    b=asyncio.create_task(endpoint(second)); await asyncio.sleep(0)
    accept_release.set(); await asyncio.sleep(0); await asyncio.sleep(0)
    assert first.accepted and not second.accepted
    await b
    assert second.closed==1008
    receive_release.set(); await a

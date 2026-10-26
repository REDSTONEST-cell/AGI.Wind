import json
import httpx
from test_client import client_app, connect


async def test_stop_cancels_pending_generation_and_revokes_approval(db, actor, seeded_scope, tmp_path, chat_request_factory):
    import asyncio
    import pytest
    from agi_wind.core.errors import AppError
    app = client_app(db, actor, tmp_path)
    service = app.state.service
    accepted = await service.submit(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id))
    task = asyncio.create_task(service.agent_approvals.request(actor, accepted.generation_id, 'list_files', {'path':'synthetic'}))
    await asyncio.sleep(0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/agent/stop', headers={'Authorization':'Bearer synthetic-private-token'})
    assert response.status_code == 200 and response.json()['stopped'] == 1
    assert db.all('generations')[0]['state'] == 'cancelled'
    with pytest.raises(AppError): await task
    assert service.agent_approvals.list(actor) == []


async def test_approval_api_auth_csrf_and_single_use(db, actor, seeded_scope, tmp_path):
    import asyncio
    from uuid import uuid4
    app = client_app(db, actor, tmp_path)
    queue = app.state.service.agent_approvals
    task = asyncio.create_task(queue.request(actor, uuid4(), 'read_file', {'path': 'synthetic.txt'}))
    await asyncio.sleep(0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/v1/agent/approvals')).status_code == 401
        _, csrf = await connect(client)
        items = (await client.get('/api/v1/agent/approvals')).json()['items']
        endpoint = '/api/v1/agent/approvals/' + items[0]['id']
        assert (await client.post(endpoint, json={'allow': True})).status_code == 403
        headers = {'Origin':'http://test', 'X-AGI-CSRF':csrf}
        assert (await client.post(endpoint, json={'allow': True}, headers=headers)).status_code == 200
        await task
        assert (await client.post(endpoint, json={'allow': True}, headers=headers)).status_code == 404
        response = await client.patch('/api/v1/agent/settings', json={'file_access_mode':'full'}, headers=headers)
        assert response.status_code == 200 and response.json()['file_access_mode'] == 'full'


async def test_admin_request_is_local_explicit_and_not_an_agent_tool(db, actor, seeded_scope, tmp_path, monkeypatch):
    import agi_wind.agent.api as api
    app = client_app(db, actor, tmp_path)
    monkeypatch.setattr(api, 'is_administrator', lambda: False)
    calls = []
    monkeypatch.setattr(api.subprocess, 'Popen', lambda args, **kw: calls.append(args))
    bearer = {'Authorization':'Bearer synthetic-private-token'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('192.0.2.1',1234)), base_url='http://test') as client:
        assert (await client.post('/api/v1/agent/admin/request', json={'confirmed': True}, headers=bearer)).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/api/v1/agent/admin/request', json={'confirmed': False}, headers=bearer)).status_code == 422
        if api.sys.platform == 'win32':
            assert (await client.post('/api/v1/agent/admin/request', json={'confirmed': True}, headers=bearer)).status_code == 200
            assert len(calls) == 1 and 'request-agent-admin.ps1' in calls[0][5]


async def test_deepseek_model_key_is_reused_without_exposing_or_copying_it(db, actor, seeded_scope, tmp_path):
    from agi_wind.core.settings import ProviderSettings
    from agi_wind.core.secrets import SecretStore
    app = client_app(db, actor, tmp_path)
    app.state.settings.providers['deepseek'] = ProviderSettings(base_url='https://api.deepseek.com', credential_ref='MODEL_DS_TEST')
    store = SecretStore(tmp_path)
    store.set('MODEL_DS_TEST', 'synthetic-model-deepseek-key')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.patch('/api/v1/agent/settings', headers={'Authorization': 'Bearer synthetic-private-token'}, json={'search_backend': 'deepseek', 'web_search_enabled': True})
    assert response.status_code == 200, response.text
    assert response.json()['search_key_inherited'] and response.json()['search_ready']
    assert 'synthetic-model-deepseek-key' not in response.text
    assert 'synthetic-model-deepseek-key' not in (tmp_path / 'settings.json').read_text(encoding='utf-8')
    assert app.state.service.agent_secret_store.get('AGI_WIND_AGENT_SEARCH_DEEPSEEK') == 'synthetic-model-deepseek-key'
    assert store.get('AGI_WIND_AGENT_SEARCH_DEEPSEEK') is None


async def test_command_settings_are_validated_and_visible(db, actor, seeded_scope, tmp_path):
    app = client_app(db, actor, tmp_path)
    bearer = {'Authorization': 'Bearer synthetic-private-token'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        initial = (await client.get('/api/v1/agent/settings', headers=bearer)).json()
        assert initial['command_access_mode'] == 'disabled'
        updated = await client.patch(
            '/api/v1/agent/settings',
            headers=bearer,
            json={'command_access_mode': 'ask', 'command_timeout_seconds': 12, 'command_max_output_chars': 1200},
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body['command_access_mode'] == 'ask'
        assert body['command_timeout_seconds'] == 12 and body['command_max_output_chars'] == 1200
        command = next(tool for tool in body['tools'] if tool['name'] == 'run_command')
        assert command['available'] is True
        assert (await client.patch('/api/v1/agent/settings', headers=bearer, json={'command_access_mode': 'root'})).status_code == 422
        assert (await client.patch('/api/v1/agent/settings', headers=bearer, json={'command_timeout_seconds': 0})).status_code == 422
        assert (await client.patch('/api/v1/agent/settings', headers=bearer, json={'command_max_output_chars': 100})).status_code == 422


async def test_agent_settings_auth_partial_update_mask_and_restart(db,actor,seeded_scope,tmp_path):
    from agi_wind.core.settings import Settings, apply_persisted_settings
    app=client_app(db,actor,tmp_path)
    bearer={'Authorization':'Bearer synthetic-private-token'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/v1/agent/settings')).status_code==401
        initial=await client.get('/api/v1/agent/settings',headers=bearer)
        assert initial.status_code==200 and initial.json()['enabled'] is False
        updated=await client.patch('/api/v1/agent/settings',headers=bearer,json={'enabled':True,'web_search_enabled':True,'search_backend':'brave','api_key':'synthetic-search-secret'})
        assert updated.status_code==200,updated.text
        assert updated.json()['search_ready'] is True
        assert updated.json()['search_key_configured'] is True
        assert 'synthetic-search-secret' not in updated.text
        assert 'synthetic-search-secret' not in (tmp_path/'settings.json').read_text(encoding='utf-8')
        second=await client.patch('/api/v1/agent/settings',headers=bearer,json={'max_rounds':2})
        assert second.json()['enabled'] is True and second.json()['max_rounds']==2
        assert second.json()['web_search_enabled'] is True
        restored=apply_persisted_settings(Settings(data_dir=tmp_path))
        assert restored.agent.enabled and restored.agent.max_rounds==2
        assert app.state.service.agent_settings().max_rounds==2
        assert (await client.patch('/api/v1/agent/settings',headers=bearer,json={'max_tool_calls':999})).status_code==422
        assert (await client.patch('/api/v1/agent/settings',headers=bearer,json={'shell_enabled':True})).status_code==422
        removed=await client.patch('/api/v1/agent/settings',headers=bearer,json={'clear_key':True})
        assert not removed.json()['search_key_configured'] and not removed.json()['search_ready']


async def test_agent_settings_cookie_write_requires_same_origin_csrf(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        _,csrf=await connect(client)
        assert (await client.patch('/api/v1/agent/settings',json={'enabled':True})).status_code==403
        assert (await client.patch('/api/v1/agent/settings',json={'enabled':True},headers={'Origin':'https://evil.invalid','X-AGI-CSRF':csrf})).status_code==403
        ok=await client.patch('/api/v1/agent/settings',json={'enabled':True},headers={'Origin':'http://test','X-AGI-CSRF':csrf})
        assert ok.status_code==200 and ok.json()['enabled']


async def test_concurrent_disjoint_agent_patches_both_survive(db,actor,seeded_scope,tmp_path,monkeypatch):
    import asyncio
    import threading
    import agi_wind.agent.api as api
    app=client_app(db,actor,tmp_path)
    bearer={'Authorization':'Bearer synthetic-private-token'}
    first_entered,release=threading.Event(),threading.Event()
    original=api.persist_settings
    def controlled(settings,*args,**kwargs):
        if settings.agent.enabled and settings.agent.max_rounds==3:
            first_entered.set()
            assert release.wait(3)
        return original(settings,*args,**kwargs)
    monkeypatch.setattr(api,'persist_settings',controlled)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        first=asyncio.create_task(client.patch('/api/v1/agent/settings',headers=bearer,json={'enabled':True}))
        assert await asyncio.to_thread(first_entered.wait,3)
        second=asyncio.create_task(client.patch('/api/v1/agent/settings',headers=bearer,json={'max_rounds':2}))
        await asyncio.sleep(0.05)
        release.set()
        responses=await asyncio.gather(first,second)
        assert all(item.status_code==200 for item in responses)
        value=(await client.get('/api/v1/agent/settings',headers=bearer)).json()
        assert value['enabled'] is True and value['max_rounds']==2

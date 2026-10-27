"""Synthetic transport regression: never opens a microphone, GPU, or model API."""
import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_barge_in_cancels_inflight_tts_and_replenishes_only_current_epoch(tmp_path, monkeypatch):
    from agi_wind.voice import call as module
    import websockets.asyncio.client

    class Upstream:
        def __init__(self): self.messages = asyncio.Queue()
        async def recv(self): return json.dumps({'type': 'ready'})
        async def send(self, message):
            if isinstance(message, str) and json.loads(message).get('type') == 'ping':
                await self.messages.put(json.dumps({'type': 'pong'}))
        def __aiter__(self): return self
        async def __anext__(self): return await self.messages.get()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    responses = []

    class Response:
        status_code = 200
        headers = {'content-type': 'audio/pcm', 'x-sample-rate': '24000'}
        def __init__(self): self.closed = False
        async def aiter_bytes(self):
            yield b'\x00\x00'
            if len(responses) == 1:
                await asyncio.Future()
        async def aclose(self): self.closed = True

    class Http:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def build_request(self, *args, **kwargs): return None
        async def send(self, *args, **kwargs):
            response = Response(); responses.append(response); return response

    monkeypatch.setattr(module.httpx, 'AsyncClient', Http)
    monkeypatch.setattr(module, 'authorized_conversation', lambda *args: None)
    monkeypatch.setattr(websockets.asyncio.client, 'connect', lambda *args, **kwargs: Upstream())
    (tmp_path / 'asr-token.txt').write_text('synthetic', encoding='utf8')
    actor = SimpleNamespace(account_id=uuid4(), user_id=uuid4())
    settings = SimpleNamespace(allowed_origins=[], data_dir=tmp_path,
                               voice=SimpleNamespace(voice_id='test', base_url='http://synthetic.invalid'))
    voice = SimpleNamespace(enabled=True, _lock=asyncio.Lock())
    db = SimpleNamespace(engine=SimpleNamespace(connect=lambda: nullcontext(None)))
    app = FastAPI()
    module.install_call_routes(app, settings, db, voice, lambda: actor)
    with TestClient(app) as client:
        ticket = client.post('/api/v1/call/ticket', json={'conversation_id': str(uuid4())}).json()['ticket']
        with client.websocket_connect('/api/v1/call/stream') as ws:
            ws.send_json({'ticket': ticket}); assert ws.receive_json()['type'] == 'ready'
            ws.send_json({'type': 'speak', 'epoch': 0, 'text': '第一段语音。'})
            assert ws.receive_json()['type'] == 'audio_start'
            assert ws.receive_bytes()[:4] == b'\x00\x00\x00\x00'
            ws.send_json({'type': 'cancel', 'epoch': 1})
            assert ws.receive_json() == {'type': 'speech_reset', 'epoch': 1, 'credits': 16}
            assert responses[0].closed
            ws.send_json({'type': 'speak', 'epoch': 1, 'text': '用户插话的新回复。'})
            assert ws.receive_json()['epoch'] == 1
            assert ws.receive_bytes()[:4] == b'\x01\x00\x00\x00'
            assert ws.receive_json()['type'] == 'audio_end'
            ws.send_json({'type': 'ping'})
            assert ws.receive_json() == {'type': 'speech_credit', 'epoch': 1, 'credits': 1}
            assert ws.receive_json()['type'] == 'pong'
            ws.send_json({'type': 'speak', 'epoch': 1, 'text': '**'})
            ws.send_json({'type': 'ping'})
            assert ws.receive_json() == {'type': 'speech_credit', 'epoch': 1, 'credits': 1}
            assert ws.receive_json()['type'] == 'pong'
    assert all(response.closed for response in responses)

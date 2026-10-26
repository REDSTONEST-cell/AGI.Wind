"""Authenticated call transport; existing chat APIs remain the archive authority."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
import json
import logging
import math
import secrets
import struct
import time
from uuid import UUID
from fastapi import Depends, WebSocket, WebSocketDisconnect
import httpx
from pydantic import Field
from ..archive.repository import authorized_conversation
from ..core.errors import AppError
from ..core.types import DTO
from .emotion import DEFAULT_EMOTION, instruction_for
from .text import clean_for_speech
ASR_HTTP = 'http://127.0.0.1:8139'
ASR_WS = 'ws://127.0.0.1:8139/stream'
logger = logging.getLogger('agi_wind.voice.call')
class CallTickets:
    def __init__(self, clock=None, limit=128):
        self.clock = clock or time.monotonic
        self.limit = limit
        self.pending = OrderedDict()
    def issue(self, owner):
        now = self.clock()
        self.pending = OrderedDict((key, value) for key, value in self.pending.items() if value[0] > now)
        while len(self.pending) >= self.limit:
            self.pending.popitem(last=False)
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = (now + 30, owner)
        return ticket
    def consume(self, ticket):
        item = self.pending.pop(ticket, None)
        return item[1] if item and item[0] > self.clock() else None
def validate_pcm(data: bytes):
    if not data or len(data) > 12800 or len(data) % 4:
        raise ValueError('invalid PCM frame')
    if any(not math.isfinite(x[0]) or abs(x[0]) > 1.01 for x in struct.iter_unpack('<f', data)):
        raise ValueError('invalid PCM samples')
class CallStart(DTO):
    conversation_id: UUID
def install_call_routes(app, settings, db, voice, authenticate):
    tickets = CallTickets()
    active = {}
    async def asr_health():
        asr = {'available':False}
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                response = await client.get(ASR_HTTP + '/health')
                if response.status_code == 200:
                    asr = {'available':True, **response.json()}
        except (httpx.HTTPError, ValueError):
            pass
        return asr
    app.state.asr_health = asr_health
    @app.get('/api/v1/call/status')
    async def status(current=Depends(authenticate)):
        asr = await app.state.asr_health()
        tts = await voice.health()
        return {'available':bool(asr.get('available') and tts.get('available')), 'asr':asr, 'tts':tts}
    @app.post('/api/v1/call/ticket')
    async def ticket(body: CallStart, current=Depends(authenticate)):
        with db.engine.connect() as conn:
            authorized_conversation(conn, current, body.conversation_id)
        return {'ticket':tickets.issue((current, body.conversation_id)), 'expires_in':30}
    @app.websocket('/api/v1/call/stream')
    async def call(ws: WebSocket):
        # Ticket is sent in the first frame, never a URL/query string or durable storage.
        origin = ws.headers.get('origin', '')
        same = ('https' if ws.url.scheme == 'wss' else 'http') + '://' + ws.url.netloc
        if origin and origin != same and origin not in settings.allowed_origins:
            await ws.close(code=1008)
            return
        await ws.accept()
        owner_key = None
        speech_task = None
        relay_task = None
        epoch = 0
        speak_queue = asyncio.Queue(maxsize=16)
        send_lock = asyncio.Lock()
        async def send(value):
            async with send_lock:
                await ws.send_json(value)
        async def audio(data, version):
            async with send_lock:
                await ws.send_bytes(struct.pack('<I', version) + data)
        async def speak_worker():
            while True:
                item = await speak_queue.get()
                version, text, emotion = item
                try:
                    # Share the normal voice lock so calls cannot overlap GPU synthesis.
                    async with voice._lock:
                        if version != epoch:
                            continue
                        started = time.perf_counter()
                        form = {'text':text, 'voice_id':settings.voice.voice_id,
                                'instruction':instruction_for(DEFAULT_EMOTION),
                                'cfg_scale':'1.0', 'seed':'42'}
                        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30, connect=3)) as client:
                            # Breeze may still be winding down an interrupted HTTP request.
                            for attempt in range(8):
                                response = await client.send(client.build_request('POST', settings.voice.base_url + '/v1/audio/speech', data=form), stream=True)
                                if response.status_code != 409:
                                    break
                                await response.aclose()
                                await asyncio.sleep(0.15)
                            try:
                                if response.status_code != 200 or 'audio/pcm' not in response.headers.get('content-type',''):
                                    raise AppError('CALL_TTS_FAILED', '通话语音暂时不可用', 503)
                                rate = int(response.headers.get('x-sample-rate', 24000))
                                await send({'type':'audio_start', 'epoch':version, 'sample_rate':rate})
                                first = True
                                async for chunk in response.aiter_bytes():
                                    if version != epoch:
                                        break
                                    if first:
                                        first = False
                                        logger.info('call tts first_audio_ms=%d', round((time.perf_counter()-started)*1000))
                                    await audio(chunk, version)
                                await send({'type':'audio_end', 'epoch':version})
                            finally:
                                await response.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning('call speech failed kind=%s', type(error).__name__)
                    await send({'type':'error', 'code':'CALL_TTS_FAILED', 'message':'语音合成失败，文字回复保留'})
                finally:
                    speak_queue.task_done()
        async def cancel_speech(version):
            nonlocal speech_task, epoch
            epoch = version
            if speech_task:
                speech_task.cancel()
                await asyncio.gather(speech_task, return_exceptions=True)
            while not speak_queue.empty():
                speak_queue.get_nowait()
                speak_queue.task_done()
            speech_task = asyncio.create_task(speak_worker())
            await send({'type':'speech_reset', 'epoch':version, 'credits':16})
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
            owner = tickets.consume(hello.get('ticket', ''))
            if owner is None:
                await ws.close(code=1008)
                return
            current, conversation_id = owner
            owner_key = (current.account_id, current.user_id)
            if owner_key in active:
                await send({'type':'error', 'code':'CALL_BUSY', 'message':'已有通话，请先挂断'})
                await ws.close(code=1008)
                return
            active[owner_key] = ws
            token = (settings.data_dir / 'asr-token.txt').read_text(encoding='utf-8').strip()
            from websockets.asyncio.client import connect
            async with connect(ASR_WS, additional_headers={'Authorization':'Bearer '+token}, open_timeout=3, max_size=65536, max_queue=8, proxy=None) as upstream:
                # websockets 15 uses the explicit proxy=None to keep localhost direct.
                await send({**json.loads(await upstream.recv()), 'speech_credits':16})
                async def relay():
                    async for message in upstream:
                        await send(json.loads(message))
                    await ws.close(code=1011)
                relay_task = asyncio.create_task(relay())
                speech_task = asyncio.create_task(speak_worker())
                frames_at = time.monotonic()
                samples_received = 0
                while True:
                    message = await asyncio.wait_for(ws.receive(), timeout=30)
                    if message['type'] == 'websocket.disconnect':
                        break
                    if message.get('bytes') is not None:
                        data = message['bytes']
                        validate_pcm(data)
                        samples_received += len(data)//4
                        # No unbounded upload / inference backlog from a malicious client.
                        if samples_received > (time.monotonic()-frames_at+2)*16000:
                            raise ValueError('audio rate exceeded')
                        await upstream.send(data)
                    elif message.get('text'):
                        if len(message['text']) > 4096:
                            raise ValueError('control too large')
                        control = json.loads(message['text'])
                        kind = control.get('type')
                        if kind == 'cancel':
                            version = control.get('epoch')
                            if type(version) is not int or not epoch <= version < 2**32:
                                raise ValueError('invalid epoch')
                            await cancel_speech(version)
                        elif kind == 'speak':
                            if not voice.enabled:
                                raise ValueError('voice disabled')
                            text = control.get('text', '')
                            if not isinstance(text, str) or len(text) > 240 or control.get('epoch') != epoch:
                                raise ValueError('invalid speech')
                            spoken = clean_for_speech(text, 240).text
                            if spoken:
                                speak_queue.put_nowait((epoch, spoken, control.get('emotion')))
                        elif kind in ('reset', 'ping'):
                            await upstream.send(json.dumps({'type':kind}))
                        else:
                            raise ValueError('invalid control')
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except Exception as error:
            logger.warning('call transport stopped kind=%s', type(error).__name__)
            try:
                await send({'type':'error', 'code':'CALL_UNAVAILABLE', 'message':'通话连接中断，请检查语音识别服务'})
                await ws.close(code=1011)
            except Exception:
                pass
        finally:
            if owner_key is not None and active.get(owner_key) is ws:
                active.pop(owner_key, None)
            for task in (speech_task, relay_task):
                if task:
                    task.cancel()
            await asyncio.gather(*(task for task in (speech_task, relay_task) if task), return_exceptions=True)

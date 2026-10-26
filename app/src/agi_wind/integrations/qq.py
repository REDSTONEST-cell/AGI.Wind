"""OneBot v11 owner-private bridge, following AstrBot/NoneBot adapter boundaries.

No upstream code is copied. QQ login/protocol stays in an external implementation.
"""
import asyncio
import base64
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from io import BytesIO
import ipaddress
import json
import logging
import secrets
import socket
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

import httpx
from fastapi import Depends, WebSocket, WebSocketDisconnect
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy import select

from ..archive import models as m
from ..archive.repository import authorized_conversation
from ..core.errors import AppError
from ..core.types import DTO, ChatRequest
from ..delivery.segments import split_reply
from .qq_groups import (ensure_group_binding, ensure_principal, group_allowed, group_message,
                        receipt_exists, save_send_receipt)
from .qq_attention import GroupAttention

logger = logging.getLogger('agi_wind.qq')
TOKEN_REF = 'AGI_WIND_QQ_ONEBOT_TOKEN'
WS_PATH = '/api/v1/integrations/qq/ws'


class QQSettings(DTO):
    enabled: bool = False
    owner_qq: str = Field(default='', pattern=r'^(?:[1-9][0-9]{4,19})?$')
    bot_qq: str = Field(default='', pattern=r'^(?:[1-9][0-9]{4,19})?$')
    conversation_id: UUID | None = None
    reply_delay_seconds: float = Field(default=0.6, ge=0, le=3)
    # Group support is opt-in per group: only explicitly enabled groups are bridged.
    group_enabled: bool = False
    allowed_groups: list[str] = Field(default_factory=list, max_length=100)
    group_trigger_mode: Literal['mention', 'smart'] = 'smart'
    followup_seconds: int = Field(default=90, ge=10, le=600)
    followup_max_rounds: int = Field(default=4, ge=1, le=20)

    @field_validator('allowed_groups')
    @classmethod
    def group_ids(cls, values):
        result = []
        for value in values:
            text = str(value).strip()
            if not text.isdigit() or not 5 <= len(text) <= 20:
                raise ValueError('group ids must be QQ group numbers')
            if text not in result:
                result.append(text)
        return result

    @model_validator(mode='after')
    def binding(self):
        if self.enabled and (not self.owner_qq or not self.bot_qq or not self.conversation_id):
            raise ValueError('enabled QQ requires owner, bot and conversation')
        if self.group_enabled and not self.enabled:
            raise ValueError('group chat requires QQ enabled')
        if self.group_enabled and not self.bot_qq:
            raise ValueError('group chat requires bot QQ')
        if self.owner_qq and self.owner_qq == self.bot_qq:
            raise ValueError('owner and bot must differ')
        return self


class QQUpdate(QQSettings):
    token: SecretStr | None = Field(default=None, min_length=32, max_length=200)


def request_id_for(bot, message_id):
    return uuid5(NAMESPACE_URL, f'agi-wind:onebot:{bot}:{message_id}')


def private_message(data, settings):
    if not isinstance(data, dict) or not settings.enabled:
        return None
    if (data.get('post_type') != 'message' or data.get('message_type') != 'private'
        or data.get('sub_type') != 'friend' or str(data.get('self_id')) != settings.bot_qq
        or str(data.get('user_id')) != settings.owner_qq or data.get('message_id') is None):
        return None
    if data.get('time') is not None:
        try:
            if abs(time.time()-float(data['time'])) > 300: return None
        except (ValueError, TypeError): return None
    segments=data.get('message')
    if not isinstance(segments,list) or len(segments)>64: return None
    text, images, image_files, records = [], [], [], []
    for part in segments:
        if not isinstance(part,dict) or not isinstance(part.get('data'),dict): continue
        value=part['data']
        if part.get('type')=='text': text.append(str(value.get('text','')))
        elif part.get('type')=='image' and len(images)+len(image_files)<4:
            url=value.get('url')
            if isinstance(url,str) and len(url)<=4000: images.append(url)
            elif isinstance(value.get('file'),str) and 0<len(value['file'])<=4000:
                image_files.append(value['file'])
        elif part.get('type')=='record' and not records:
            file=value.get('file')
            if isinstance(file,str) and 0<len(file)<=4000: records.append(file)
    text=''.join(text).strip()
    if len(text)>12000 or (not text and not images and not image_files and not records): return None
    return {'text':text,'images':images,'image_files':image_files,'records':records,
            'platform_message_id':str(data['message_id']),
            'request_id':request_id_for(settings.bot_qq,data['message_id'])}


class QQConnection:
    def __init__(self, websocket):
        self.websocket=websocket
        self.pending={}
        self.connected=True
        self.lock=asyncio.Lock()

    async def call(self, action, params):
        if not self.connected: raise ConnectionError('QQ disconnected')
        echo=uuid4().hex
        future=asyncio.get_running_loop().create_future()
        self.pending[echo]=future
        try:
            async with self.lock:
                await self.websocket.send_json({'action':action,'params':params,'echo':echo})
            response=await asyncio.wait_for(future,timeout=15)
            if response.get('status')!='ok' or response.get('retcode')!=0:
                raise RuntimeError('OneBot action failed')
            return response.get('data') or {}
        finally:
            self.pending.pop(echo,None)

    def resolve(self, data):
        echo=data.get('echo')
        if isinstance(echo,str) and (future:=self.pending.get(echo)) and not future.done():
            future.set_result(data)

    def disconnect(self):
        self.connected=False
        for future in self.pending.values():
            if not future.done(): future.set_exception(ConnectionError('QQ disconnected'))
        self.pending.clear()


async def download_image(url, limit):
    """Only QQ HTTPS media; pin the verified IP while preserving TLS hostname."""
    parsed=urlsplit(url)
    host=(parsed.hostname or '').lower()
    allowed=(host=='qpic.cn' or host.endswith('.qpic.cn') or host in {'multimedia.nt.qq.com','multimedia.nt.qq.com.cn'})
    # QQ still reports some media as HTTP. Upgrade only known QQ hosts, never
    # perform a plaintext request or follow a redirect to an arbitrary host.
    if parsed.scheme=='http' and allowed and parsed.port is None:
        url=parsed._replace(scheme='https').geturl()
        parsed=urlsplit(url)
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443)
        or not allowed):
        raise AppError('QQ_IMAGE_URL','只接收QQ公网HTTPS图片地址',422)
    addresses=await asyncio.to_thread(socket.getaddrinfo,host,443,type=socket.SOCK_STREAM)
    ips=list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise AppError('QQ_IMAGE_URL','图片地址无效',422)
    ip=ips[0]
    target=httpx.URL(url).copy_with(host=ip)
    data=bytearray()
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=15) as client:
        async with client.stream('GET',target,headers={'Host':host},extensions={'sni_hostname':host}) as response:
            if response.status_code!=200: raise AppError('QQ_IMAGE_DOWNLOAD','QQ图片下载失败',422)
            mime=response.headers.get('content-type','').split(';')[0].strip()
            if mime not in {'image/png','image/jpeg','image/gif','image/webp'}:
                raise AppError('QQ_IMAGE_TYPE','不支持的QQ图片类型',422)
            async for block in response.aiter_bytes():
                data.extend(block)
                if len(data)>limit: raise AppError('QQ_IMAGE_SIZE','QQ图片超过大小限制',413)
    return bytes(data),mime


class QQBridge:
    def __init__(self, app, settings, store, service, assets, actor, wakeup):
        self.app,self.settings,self.store,self.service,self.assets,self.actor=app,settings,store,service,assets,actor
        self.wakeup=wakeup
        self.connection=None
        self.job=None
        self.generation_id=None
        self.queue=asyncio.Queue(maxsize=4)
        self.seen=deque(maxlen=1024)
        self.last_status='disabled'
        self.stopping=False
        self.stop_lock=asyncio.Lock()
        self.connection_lock=asyncio.Lock()
        self.attention=GroupAttention(settings.qq.followup_seconds,settings.qq.followup_max_rounds)

    def view(self):
        return {**self.settings.qq.model_dump(mode='json'),'token_configured':self.store.configured(TOKEN_REF),
                'connected':bool(self.connection and self.connection.connected),'status':self.last_status,
                'ws_path':WS_PATH,'groups_enabled':self.settings.qq.group_enabled,'queued':self.queue.qsize()}

    async def stop(self):
        async with self.stop_lock:
            self.stopping=True
            try: await self._stop()
            finally: self.stopping=False

    async def _stop(self):
        # Drain before the first await so the consumer cannot start the next job
        # while interrupt() waits for an upstream provider to finish cancelling.
        while not self.queue.empty(): self.queue.get_nowait(); self.queue.task_done()
        job=self.job
        generation_id=self.generation_id
        if job and not job.done(): job.cancel()
        if generation_id:
            await self.service.interrupt(self.actor,generation_id,uuid4())
        approvals=getattr(self.service,'agent_approvals',None)
        if approvals and generation_id: approvals.revoke(self.actor,generation_id=generation_id)
        if job:
            await asyncio.gather(job,return_exceptions=True)
        self.generation_id=None

    async def close(self):
        connection=self.connection
        if connection:
            connection.disconnect()
        await self.stop()
        if connection:
            with suppress(Exception): await connection.websocket.close(code=1001)

    async def receive_message(self, data):
        message=private_message(data,self.settings.qq)
        if message:
            if message['text'].strip().lower() in ('stop','/stop','停止','/停止'):
                await self.stop()
                self.last_status='stopped'
                return
            if self.stopping: return
            rid=message['request_id']
            if rid in self.seen: return
            with self.service.db.engine.connect() as conn:
                if conn.scalar(select(m.requests.c.id).where(m.requests.c.id==rid)): return
            if self.queue.full():
                self.last_status='queue_full'
                return
            self.seen.append(rid)
            self.queue.put_nowait(message)
            return
        group=group_message(data,self.settings.qq)
        if not group: return
        if self.stopping: return
        if not group_allowed(self.settings.qq,group['group_id']):
            self.last_status='group_ignored'
            return
        key=f"group:{group['bot_qq']}:{group['group_id']}:{group['platform_message_id']}"
        if key in self.seen: return
        if self.queue.full():
            self.last_status='queue_full'
            return
        self.seen.append(key)
        self.queue.put_nowait(group)

    async def consume(self, connection):
        while connection.connected:
            message=await self.queue.get()
            try:
                if self.stopping or not connection.connected: continue
                self.job=asyncio.create_task(self.respond(connection,message))
                await asyncio.gather(self.job,return_exceptions=True)
            finally:
                self.job=None; self.generation_id=None; self.queue.task_done()

    async def respond(self, connection, message):
        if message.get('kind') == 'group':
            return await self.respond_group(connection, message)
        return await self.respond_private(connection, message)

    async def respond_private(self, connection, message):
        try:
            policy=self.settings.qq
            if not policy.enabled or connection is not self.connection: return
            with self.service.db.engine.connect() as conn:
                conversation=authorized_conversation(conn,self.actor,policy.conversation_id)
                if conversation['state']!='active': raise AppError('QQ_CONVERSATION','QQ绑定的对话已归档，请重新选择',409)
            text,asset_ids=await self._prepare_input(connection,message)
            request=ChatRequest(request_id=message['request_id'],conversation_id=policy.conversation_id,
                input={'text':text,'asset_ids':asset_ids},output={'voice':self._voice_mode(message)},device_id='qq-onebot')
            accepted=await self.service.submit(self.actor,request)
            self.generation_id=accepted.generation_id
            self.wakeup.set(); self.last_status='generating'
            row=await self._await_generation()
            if row['state']!='completed' or row['delivery_state']=='cancelled':
                self.last_status='generation_stopped'; return
            parts=await self._reply_parts(await self._reply_content())
            for index,part in enumerate(parts[:32]):
                if index: await asyncio.sleep(policy.reply_delay_seconds)
                if not self.settings.qq.enabled or connection is not self.connection: return
                await connection.call('send_private_msg',{'user_id':int(policy.owner_qq),'message':[part]})
            self.last_status='ready'
            logger.info('qq reply delivered request=%s segments=%s',request.request_id,min(32,len(parts)))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_status='failed'
            logger.warning('qq operation failed type=%s',type(error).__name__)
            if isinstance(error,AppError) and error.code.startswith('QQ_'):
                with suppress(Exception):
                    await connection.call('send_private_msg',{'user_id':int(self.settings.qq.owner_qq),
                        'message':[{'type':'text','data':{'text':'这条图片或语音没有读取成功，请稍后重发'}}]})
            if self.generation_id:
                with suppress(Exception): await self.service.interrupt(self.actor,self.generation_id,uuid4())

    async def respond_group(self, connection, message):
        """Reply in one group room after the rule-first attention decision."""
        try:
            policy=self.settings.qq
            if not policy.enabled or connection is not self.connection: return
            room_key=None
            with self.service.db.engine.connect() as conn:
                private=authorized_conversation(conn,self.actor,policy.conversation_id)
                companion_id=private['companion_id']
            principal=None
            if not message.get('anonymous'):
                principal=ensure_principal(self.service.db, self.actor,
                    external_user_id=message['sender_qq'], nickname=message.get('sender_nickname',''),
                    card=message.get('sender_card',''), is_owner=message['sender_qq']==policy.owner_qq)
            binding=ensure_group_binding(self.service.db, self.actor, companion_id=companion_id,
                bot_qq=message['bot_qq'], group_id=message['group_id'],
                title=message.get('group_name',''))
            conversation_id=binding['conversation_id']
            room_key=str(binding['id'])
            text,asset_ids=await self._prepare_input(connection,message)
            message={**message,'text':text}
            occurred_at=None
            if message.get('occurred_at') is not None:
                occurred_at=datetime.fromtimestamp(float(message['occurred_at']),tz=timezone.utc)
            event=self.service.archive.record_external_message(self.actor, conversation_id,
                text=message['text'], asset_ids=asset_ids,
                sender_principal_id=principal['id'] if principal else None,
                external_message_id=message['platform_message_id'],
                reply_to_external_id=message.get('reply_to_platform_message_id'), occurred_at=occurred_at)
            if event.get('request_id') is not None:
                self.last_status='group_duplicate'
                return
            receipt=False
            if message.get('reply_to_platform_message_id'):
                receipt=receipt_exists(self.service.db, binding['id'], message['reply_to_platform_message_id'])
            decision=self.attention.decide({
                'room_key': room_key, 'sender_principal_id': str(principal['id']) if principal else None,
                'is_self': False, 'anonymous': bool(message.get('anonymous')),
                'bot_qq': message['bot_qq'], 'mentioned_qq_ids': message.get('mentioned_qq_ids') or [],
                'reply_to_platform_message_id': message.get('reply_to_platform_message_id'),
                'text': message['text'], 'platform_message_id': message['platform_message_id'],
            }, receipt_exists=receipt, trigger_mode=policy.group_trigger_mode)
            if decision.action!='reply':
                self.last_status='group_observed'
                logger.info('qq group observed group=%s sender=%s reason=%s',message['group_id'],
                    message['sender_qq'],decision.reason)
                try:
                    if getattr(self.service, 'memory_extraction_enabled', False):
                        from ..core.types import SceneScope
                        from ..memory.jobs import enqueue_member_extraction
                        with self.service.db.engine.begin() as db_conn:
                            enqueue_member_extraction(db_conn, SceneScope(
                                account_id=self.actor.account_id, user_id=self.actor.user_id,
                                companion_id=companion_id, conversation_id=conversation_id,
                                scene='group', room_id=binding['id']), observed=True)
                except Exception:
                    logger.debug('could not queue observed member memory extraction', exc_info=True)
                return
            request_id=uuid5(NAMESPACE_URL, f"agi-wind:qq-group:{message['bot_qq']}:{message['group_id']}:{message['platform_message_id']}")
            accepted=self.service.archive.submit_external_reply(self.actor, conversation_id, event['id'], request_id,
                output={'voice':self._voice_mode(message)}, device_id='qq-group')
            self.generation_id=accepted.generation_id
            self.wakeup.set(); self.last_status='generating'
            row=await self._await_generation()
            if row['state']!='completed' or row['delivery_state']=='cancelled':
                self.last_status='generation_stopped'; return
            parts=await self._reply_parts(await self._reply_content())
            bot_message_ids=[]
            for index,part in enumerate(parts[:32]):
                if index: await asyncio.sleep(policy.reply_delay_seconds)
                if not self.settings.qq.enabled or connection is not self.connection: return
                result=await connection.call('send_group_msg',{'group_id':int(message['group_id']),'message':[part]})
                platform_message_id=result.get('message_id') if isinstance(result,dict) else None
                if platform_message_id is not None:
                    save_send_receipt(self.service.db, binding['id'], platform_message_id,
                        generation_id=accepted.generation_id, segment_index=index)
                    bot_message_ids.append(str(platform_message_id))
            if bot_message_ids:
                self.attention.mark_replied(room_key, str(principal['id']) if principal else None, bot_message_ids)
            self.last_status='ready'
            logger.info('qq group reply delivered group=%s sender=%s reason=%s segments=%s',
                message['group_id'],message['sender_qq'],decision.reason,min(32,len(parts)))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_status='failed'
            logger.warning('qq group operation failed group=%s type=%s',message.get('group_id'),type(error).__name__)
            if self.generation_id:
                with suppress(Exception): await self.service.interrupt(self.actor,self.generation_id,uuid4())

    async def _await_generation(self):
        async with asyncio.timeout(300):
            while True:
                with self.service.db.engine.connect() as conn:
                    row=conn.execute(select(m.generations).where(m.generations.c.id==self.generation_id)).mappings().one()
                if row['state'] in ('completed','cancelled','failed','interrupted'): return row
                await asyncio.sleep(0.15)

    def _assistant_content(self):
        with self.service.db.engine.connect() as conn:
            return conn.scalar(select(m.events.c.content).where(m.events.c.generation_id==self.generation_id,
                m.events.c.role=='assistant',m.events.c.kind=='assistant_output')) or {}

    @staticmethod
    def _voice_mode(message):
        return 'always' if message.get('records') or '语音' in message.get('text','') else 'auto'

    async def _prepare_input(self, connection, message):
        text=message.get('text','')
        if message.get('records'):
            # SnowLuma resolves the original QQ message and performs QQ's PTT
            # transcription. Never fetch a record URL or open an event-supplied path.
            try:
                result=await connection.call('get_record_text',{'message_id':message['platform_message_id']})
                transcript=result.get('text')
                if not isinstance(transcript,str) or not transcript.strip() or len(transcript)>12000:
                    raise ValueError('invalid transcript')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise AppError('QQ_VOICE_TRANSCRIBE','QQ语音识别失败，请重发或改发文字',422) from error
            text=(text+'\n[语音转文字] '+transcript.strip()).strip()
            if len(text)>12000:
                raise AppError('QQ_VOICE_SIZE','语音转写内容过长',413)
        urls=list(message.get('images') or [])
        for file in message.get('image_files') or []:
            info=await connection.call('get_image',{'file':file})
            url=info.get('url') or info.get('file')
            if not isinstance(url,str) or len(url)>4000:
                raise AppError('QQ_IMAGE_URL','QQ图片没有可下载地址',422)
            urls.append(url)
        asset_ids=[]
        for url in urls[:4]:
            raw,mime=await download_image(url,min(self.assets.max_image_bytes,10*1024*1024))
            asset=await self.assets.put(self.actor,BytesIO(raw),mime)
            asset_ids.append(asset['id'])
        return text,asset_ids

    async def _reply_content(self):
        # Text generation ends before background TTS. Wait only for this turn,
        # bounded and cancellable; failed/slow speech still delivers the text.
        deadline=time.monotonic()+120
        while True:
            content=self._assistant_content()
            pending=any(item.get('state') in ('queued','synthesizing') for item in content.get('voices') or [])
            if not pending or time.monotonic()>=deadline: return content
            await asyncio.sleep(0.2)

    async def _reply_parts(self, content):
        parts=[{'type':'text','data':{'text':text}} for text,_ in split_reply(content.get('text',''))]
        for item in content.get('attachments') or []:
            kind=item.get('kind')
            if kind not in ('image','audio'): continue
            asset,path=self.assets.get(self.actor,UUID(item['asset_id']))
            if asset['kind']!=kind or asset['bytes']>10*1024*1024: continue
            raw=await asyncio.to_thread(path.read_bytes)
            parts.append({'type':'record' if kind=='audio' else 'image',
                          'data':{'file':'base64://'+base64.b64encode(raw).decode()}})
        return parts


def install_qq_routes(app,settings,store,service,assets,actor,authenticate,wakeup):
    from ..core.settings import persist_settings,SETTINGS_WRITE_LOCK
    bridge=QQBridge(app,settings,store,service,assets,actor,wakeup)
    app.state.qq_bridge=bridge

    @app.get('/api/v1/integrations/qq')
    async def status(current=Depends(authenticate)):
        return bridge.view()

    @app.get('/api/v1/integrations/qq/groups')
    def groups(companion_id:UUID,current=Depends(authenticate)):
        """Persistent group windows for the current companion (read-only listing)."""
        from ..identity.service import authorize as authorize_companion
        with service.db.engine.connect() as conn:
            authorize_companion(conn,current,companion_id)
            rows=conn.execute(select(m.channel_bindings,m.conversations.c.title.label('conversation_title'),
                m.conversations.c.state.label('conversation_state'))
                .join(m.conversations,m.channel_bindings.c.conversation_id==m.conversations.c.id)
                .where(m.channel_bindings.c.account_id==current.account_id,
                       m.channel_bindings.c.companion_id==companion_id,
                       m.channel_bindings.c.channel_kind=='group')
                .order_by(m.channel_bindings.c.created_at)).mappings()
            return [{'binding_id':str(row['id']),'conversation_id':str(row['conversation_id']),
                     'group_id':row['external_chat_id'],'title':row['title'] or row['conversation_title'],
                     'enabled':bool(row['enabled']),'conversation_state':row['conversation_state'],
                     'created_at':row['created_at']} for row in rows]

    @app.put('/api/v1/integrations/qq')
    async def update(body:QQUpdate,current=Depends(authenticate)):
        policy=QQSettings.model_validate(body.model_dump(exclude={'token'}))
        if policy.conversation_id:
            with service.db.engine.connect() as conn:
                row=authorized_conversation(conn,current,policy.conversation_id)
                if row['state']!='active': raise AppError('QQ_CONVERSATION','请选择当前活动对话',409)
        token=body.token.get_secret_value() if body.token else store.get(TOKEN_REF)
        if policy.enabled and not token: raise AppError('QQ_TOKEN_REQUIRED','请先设置至少32位连接令牌',422)
        async with bridge.connection_lock:
            await bridge.close()
            with SETTINGS_WRITE_LOCK:
                previous=settings.qq
                try:
                    if body.token: store.set(TOKEN_REF,token)
                    settings.qq=policy
                    persist_settings(settings)
                except Exception:
                    settings.qq=previous
                    raise
        bridge.last_status='waiting' if policy.enabled else 'disabled'
        bridge.attention=GroupAttention(policy.followup_seconds,policy.followup_max_rounds)
        return bridge.view()

    @app.websocket(WS_PATH)
    async def websocket(websocket:WebSocket):
        supplied=websocket.headers.get('authorization','')
        async with bridge.connection_lock:
            policy=settings.qq; token=store.get(TOKEN_REF)
            if (not policy.enabled or not token or not secrets.compare_digest(supplied,'Bearer '+token)
                or websocket.headers.get('x-self-id')!=policy.bot_qq
                or websocket.headers.get('x-client-role','Universal').lower()!='universal'
                or websocket.headers.get('origin') or bridge.connection is not None):
                await websocket.close(code=1008); return
            await websocket.accept()
            connection=QQConnection(websocket); bridge.connection=connection; bridge.last_status='ready'
        worker=asyncio.create_task(bridge.consume(connection))
        logger.info('qq onebot connected')
        try:
            while True:
                raw=await websocket.receive_text()
                if len(raw.encode())>256*1024: await websocket.close(code=1009); break
                data=json.loads(raw)
                if not isinstance(data,dict): continue
                if 'echo' in data: connection.resolve(data)
                else: await bridge.receive_message(data)
        except (WebSocketDisconnect,ValueError): pass
        finally:
            connection.disconnect()
            if bridge.connection is connection: await bridge.stop()
            worker.cancel(); await asyncio.gather(worker,return_exceptions=True)
            if bridge.connection is connection: bridge.connection=None
            bridge.last_status='waiting' if settings.qq.enabled else 'disabled'
            logger.info('qq onebot disconnected')

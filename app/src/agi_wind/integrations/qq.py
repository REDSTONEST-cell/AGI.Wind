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
import math
import random
import secrets
import socket
import time
from anyio import CancelScope
from pathlib import Path
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
from .qq_debounce import QQDebouncer, config_from_policy

logger = logging.getLogger('agi_wind.qq')
TOKEN_REF = 'AGI_WIND_QQ_ONEBOT_TOKEN'
WS_PATH = '/api/v1/integrations/qq/ws'


class QQSettings(DTO):
    enabled: bool = False
    owner_qq: str = Field(default='', pattern=r'^(?:[1-9][0-9]{4,19})?$')
    bot_qq: str = Field(default='', pattern=r'^(?:[1-9][0-9]{4,19})?$')
    conversation_id: UUID | None = None
    # Base pause before each outgoing bubble. The actual delay also uses a log
    # curve on text length plus jitter; 0 disables pacing (tests use this).
    reply_delay_seconds: float = Field(default=1.0, ge=0, le=3)
    reply_min_gap_seconds: float = Field(default=1.8, ge=0, le=10)
    reply_max_delay_seconds: float = Field(default=8.0, ge=2, le=20)
    reply_max_segments: int = Field(default=8, ge=1, le=32)
    # Private-message aggregation. Group aggregation stays disabled because
    # per-sender batches would reorder a shared group scope.
    debounce_enabled: bool = True
    debounce_quiet_seconds: float = Field(default=2.2, ge=0.2, le=10)
    debounce_max_wait_seconds: float = Field(default=8.0, ge=0.5, le=30)
    debounce_final_grace_seconds: float = Field(default=0.9, ge=0.05, le=5)
    debounce_continuation_seconds: float = Field(default=2.0, ge=0.2, le=10)
    debounce_max_messages: int = Field(default=8, ge=1, le=20)
    debounce_max_chars: int = Field(default=4000, ge=100, le=12000)
    debounce_min_chars: int = Field(default=2, ge=1, le=200)
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
        if self.debounce_quiet_seconds > self.debounce_max_wait_seconds:
            raise ValueError('debounce quiet window cannot exceed max wait')
        return self


class QQUpdate(QQSettings):
    token: SecretStr | None = Field(default=None, min_length=32, max_length=200)


class QQGroupToggle(DTO):
    enabled: bool


class QQOutboxFile(DTO):
    """A file from the dedicated QQ outbox directory; never an arbitrary path."""
    name: str = Field(min_length=1, max_length=200)


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
        self.send_lock=asyncio.Lock()
        self.send_version=0
        self._last_send_at=0.0
        self.debouncer=QQDebouncer(
            self._enqueue_debounced_private,
            config_provider=lambda: config_from_policy(self.settings.qq),
        )
        data_dir=getattr(settings,'data_dir',None)
        self.attention=GroupAttention(settings.qq.followup_seconds,settings.qq.followup_max_rounds,
            state_path=data_dir/'qq-attention.json' if data_dir is not None else None)

    def view(self):
        return {**self.settings.qq.model_dump(mode='json'),'token_configured':self.store.configured(TOKEN_REF),
                'connected':bool(self.connection and self.connection.connected),'status':self.last_status,
                'ws_path':WS_PATH,'groups_enabled':self.settings.qq.group_enabled,'queued':self.queue.qsize(),
                'debouncing':self.debouncer.pending_count()}

    async def stop(self):
        async with self.stop_lock:
            self.stopping=True
            try: await self._stop()
            finally: self.stopping=False

    async def _stop(self):
        self.send_version+=1
        idle=getattr(self.service,'idle_cognition',None)
        if idle is not None:
            idle.cancel_actor(self.actor,'stopped')
        await self.debouncer.cancel_all("bridge_stopped")
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
                stop_all=getattr(self.service,'stop_all',None)
                if stop_all:
                    await stop_all(self.actor)
                else:
                    await self.stop()
                self.last_status='stopped'
                return
            if self.stopping: return
            rid=message['request_id']
            if rid in self.seen: return
            with self.service.db.engine.connect() as conn:
                if conn.scalar(select(m.requests.c.id).where(m.requests.c.id==rid)): return
            idle=getattr(self.service,'idle_cognition',None)
            if idle is not None and self.settings.qq.conversation_id:
                idle.cancel_conversation(self.settings.qq.conversation_id,'user_message')
            if self._can_debounce(message):
                key=f"private:{self.settings.qq.bot_qq}:{self.settings.qq.owner_qq}:{self.settings.qq.conversation_id}"
                accepted=await self.debouncer.push(key,message)
                if accepted:
                    self.seen.append(rid)
                    self.last_status='debouncing'
                    return
            else:
                # A command/media message is a boundary: flush pending text
                # fragments first so the user-visible order is not inverted.
                key=f"private:{self.settings.qq.bot_qq}:{self.settings.qq.owner_qq}:{self.settings.qq.conversation_id}"
                await self.debouncer.flush_key(key,'barrier')
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
        if group.get('sender_qq')==self.settings.qq.owner_qq and str(group.get('text') or '').strip().lower() in ('stop','/stop','停止','/停止'):
            stop_all=getattr(self.service,'stop_all',None)
            if stop_all:
                await stop_all(self.actor)
            else:
                await self.stop()
            self.last_status='stopped'
            return
        key=f"group:{group['bot_qq']}:{group['group_id']}:{group['platform_message_id']}"
        if key in self.seen: return
        idle=getattr(self.service,'idle_cognition',None)
        if idle is not None:
            with self.service.db.engine.connect() as conn:
                conversation_id=conn.scalar(select(m.channel_bindings.c.conversation_id).where(
                    m.channel_bindings.c.account_id==self.actor.account_id,
                    m.channel_bindings.c.companion_id==select(m.conversations.c.companion_id).where(
                        m.conversations.c.id==self.settings.qq.conversation_id).scalar_subquery(),
                    m.channel_bindings.c.platform=='qq', m.channel_bindings.c.channel_kind=='group',
                    m.channel_bindings.c.bot_external_id==group['bot_qq'],
                    m.channel_bindings.c.external_chat_id==group['group_id'],
                    m.channel_bindings.c.enabled.is_(True)))
            if conversation_id is not None:
                idle.cancel_conversation(conversation_id,'user_message')
        if self.queue.full():
            self.last_status='queue_full'
            return
        self.seen.append(key)
        self.queue.put_nowait(group)

    def _can_debounce(self, message):
        """Only ordinary private text participates; media and commands stay immediate."""
        policy=self.settings.qq
        text=str(message.get('text') or '').strip()
        return bool(policy.debounce_enabled and text and not text.startswith(('/','#','!','！'))
                    and not message.get('images') and not message.get('image_files')
                    and not message.get('records'))

    async def _enqueue_debounced_private(self, batch, reason):
        if self.stopping or not self.connection or not self.connection.connected:
            return
        payload={'kind':'private_batch','messages':list(batch.messages),'text':batch.text,
                 'debounce_reason':reason}
        # A bounded queue is fine here: waiting in the debounce task never holds
        # the send lock or a generation worker, and stop() cancels this task.
        await self.queue.put(payload)
        self.last_status='ready'
        logger.info('qq debounce flushed messages=%s chars=%s reason=%s',
                    len(batch.messages),len(batch.text),reason)

    async def consume(self, connection):
        while connection.connected:
            message=await self.queue.get()
            try:
                if self.stopping or not connection.connected: continue
                async with self.send_lock:
                    if self.stopping or not connection.connected or connection is not self.connection: continue
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
            messages=message.get('messages') if isinstance(message.get('messages'),list) else [message]
            prepared=[]
            for item in messages:
                prepared.append(await self._prepare_input(connection,item))
            text='\n'.join(value for value,_ in prepared if value.strip())
            asset_ids=list(dict.fromkeys(asset_id for _,values in prepared for asset_id in values))
            voice_mode=('always' if any(item.get('records') for item in messages) or '语音' in text else 'auto')
            request=ChatRequest(request_id=messages[0].get('request_id') or message['request_id'],
                conversation_id=policy.conversation_id,
                input={'text':text,'asset_ids':asset_ids},output={'voice':voice_mode},
                device_id=f'qq-private:{policy.bot_qq}:{policy.owner_qq}')
            accepted=await self.service.submit(self.actor,request)
            self.generation_id=accepted.generation_id
            self.wakeup.set(); self.last_status='generating'
            row=await self._await_generation()
            if row['state']!='completed' or row['delivery_state']=='cancelled':
                self.last_status='generation_stopped'; return
            parts=await self._reply_parts(await self._reply_content())
            max_parts=max(1,min(32,int(policy.reply_max_segments)))
            for index,part in enumerate(parts[:max_parts]):
                await self._pause_before_send(part,policy,first=index==0)
                current=self.settings.qq
                if (not current.enabled or not connection.connected or connection is not self.connection
                        or (current.bot_qq,current.owner_qq,current.conversation_id)!=(policy.bot_qq,policy.owner_qq,policy.conversation_id)): return
                await connection.call('send_private_msg',{'user_id':int(policy.owner_qq),'message':[part]})
                self._mark_sent()
            self.last_status='ready'
            logger.info('qq reply delivered request=%s messages=%s segments=%s',
                        request.request_id,len(messages),min(max_parts,len(parts)))
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
            if (not policy.enabled or connection is not self.connection
                    or not group_allowed(policy,message['group_id']) or message['bot_qq']!=policy.bot_qq): return
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
            # Only this room's recent events enter attention classification.
            with self.service.db.engine.connect() as conn:
                recent=list(conn.execute(select(m.events.c.sender_principal_id,m.events.c.role,m.events.c.content).where(
                    m.events.c.account_id==self.actor.account_id,m.events.c.conversation_id==conversation_id,
                    m.events.c.id!=event['id'],m.events.c.visibility=='visible',
                    m.events.c.role.in_(('user','assistant'))).order_by(
                        m.events.c.recorded_at.desc(),m.events.c.id.desc()).limit(6)).mappings())
            context=[{'sender_principal_id':'bot' if row['role']=='assistant' else str(row['sender_principal_id'] or ''),
                      'text':str((row['content'] or {}).get('text') or '')[:200]} for row in reversed(recent)]
            self.attention.followup_seconds=policy.followup_seconds
            self.attention.max_rounds=policy.followup_max_rounds
            decision=await self.attention.decide_smart({
                'room_key': room_key, 'sender_principal_id': str(principal['id']) if principal else None,
                'is_self': False, 'anonymous': bool(message.get('anonymous')),
                'bot_qq': message['bot_qq'], 'mentioned_qq_ids': message.get('mentioned_qq_ids') or [],
                'reply_to_platform_message_id': message.get('reply_to_platform_message_id'),
                'text': message['text'], 'platform_message_id': message['platform_message_id'],
            }, receipt_exists=receipt, trigger_mode=policy.group_trigger_mode,
                provider=getattr(self.service,'provider',None),model=getattr(self.service,'model',None),context=context)
            if decision.action!='reply':
                self.last_status='group_observed'
                logger.info('qq group observed group=%s sender=%s reason=%s',message['group_id'],
                    message['sender_qq'],decision.reason)
                try:
                    if getattr(self.service, 'memory_extraction_enabled', False):
                        from ..core.types import SceneScope
                        from ..memory.jobs import enqueue_member_extraction, enqueue_extraction
                        with self.service.db.engine.begin() as db_conn:
                            memory_scope=SceneScope(
                                account_id=self.actor.account_id, user_id=self.actor.user_id,
                                companion_id=companion_id, conversation_id=conversation_id,
                                scene='group', room_id=binding['id'])
                            if principal and principal['is_owner']:
                                enqueue_extraction(db_conn,memory_scope,event['id'])
                            elif principal:
                                enqueue_member_extraction(db_conn,memory_scope,observed=True)
                except Exception:
                    logger.debug('could not queue observed member memory extraction', exc_info=True)
                return
            request_id=uuid5(NAMESPACE_URL, f"agi-wind:qq-group:{message['bot_qq']}:{message['group_id']}:{message['platform_message_id']}")
            accepted=self.service.archive.submit_external_reply(self.actor, conversation_id, event['id'], request_id,
                output={'voice':self._voice_mode(message)}, device_id=f"qq-group:{message['bot_qq']}:{message['group_id']}")
            self.generation_id=accepted.generation_id
            self.wakeup.set(); self.last_status='generating'
            row=await self._await_generation()
            if row['state']!='completed' or row['delivery_state']=='cancelled':
                self.last_status='generation_stopped'; return
            parts=await self._reply_parts(await self._reply_content())
            bot_message_ids=[]
            max_parts=max(1,min(32,int(policy.reply_max_segments)))
            for index,part in enumerate(parts[:max_parts]):
                await self._pause_before_send(part,policy,first=index==0)
                current=self.settings.qq
                if (not current.enabled or not connection.connected or connection is not self.connection
                        or not group_allowed(current,message['group_id']) or current.bot_qq!=message['bot_qq']
                        or current.conversation_id!=policy.conversation_id): return
                result=await connection.call('send_group_msg',{'group_id':int(message['group_id']),'message':[part]})
                self._mark_sent()
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

    def _proactive_binding(self, target):
        """Resolve the current authorized room; targets never create new bindings."""
        policy=self.settings.qq
        connection=self.connection
        if (not isinstance(target,dict) or not policy.enabled or self.stopping
                or not connection or not connection.connected or target.get('bot')!=policy.bot_qq):
            return None
        kind=target.get('kind'); recipient=str(target.get('recipient') or '')
        with self.service.db.engine.connect() as conn:
            private=authorized_conversation(conn,self.actor,policy.conversation_id)
            if private['state']!='active': return None
            if kind=='private':
                if recipient!=policy.owner_qq: return None
                conversation_id=private['id']; binding=None
            elif kind=='group':
                if not group_allowed(policy,recipient): return None
                binding=conn.execute(select(m.channel_bindings).where(
                    m.channel_bindings.c.account_id==self.actor.account_id,
                    m.channel_bindings.c.companion_id==private['companion_id'],
                    m.channel_bindings.c.platform=='qq',m.channel_bindings.c.channel_kind=='group',
                    m.channel_bindings.c.bot_external_id==policy.bot_qq,
                    m.channel_bindings.c.external_chat_id==recipient,
                    m.channel_bindings.c.enabled.is_(True))).mappings().first()
                if binding is None: return None
                conversation=authorized_conversation(conn,self.actor,binding['conversation_id'])
                if conversation['state']!='active': return None
                conversation_id=conversation['id']
                if target.get('binding_id') and str(binding['id'])!=str(target['binding_id']): return None
            else:
                return None
            if target.get('conversation_id') and str(conversation_id)!=str(target['conversation_id']): return None
            return {'conversation_id':conversation_id,'binding_id':binding['id'] if binding else None}

    def proactive_available(self, target):
        try:
            return self._proactive_binding(target) is not None
        except Exception:
            return False

    async def deliver_proactive(self, target, content, result, check):
        """Serialize bounded QQ segments and recheck cancellation and binding before each send."""
        connection=self.connection; version=self.send_version; sent=0
        async with self.send_lock:
            binding=self._proactive_binding(target)
            if not binding or connection is not self.connection or version!=self.send_version:
                return {'sent':False,'reason':'qq_unavailable','segments':0}
            if str(result.get('conversation_id') or '')!=str(binding['conversation_id']):
                return {'sent':False,'reason':'qq_binding_changed','segments':0}
            if await check() is False:
                return {'sent':False,'reason':'qq_cancelled','segments':0}
            parts=await self._reply_parts(content)
            ids=[]
            policy=self.settings.qq
            max_parts=max(1,min(32,int(policy.reply_max_segments)))
            for index,part in enumerate(parts[:max_parts]):
                await self._pause_before_send(part,policy,first=index==0)
                if await check() is False:
                    return {'sent':False,'reason':'qq_cancelled','segments':sent}
                current=self._proactive_binding(target)
                if (current!=binding or connection is not self.connection or not connection.connected
                        or version!=self.send_version):
                    return {'sent':False,'reason':'qq_binding_changed','segments':sent}
                kind=target['kind']
                delivered=await connection.call('send_group_msg' if kind=='group' else 'send_private_msg',
                    {'group_id' if kind=='group' else 'user_id':int(target['recipient']),'message':[part]})
                self._mark_sent()
                sent+=1
                if kind=='group' and isinstance(delivered,dict) and delivered.get('message_id') is not None:
                    save_send_receipt(self.service.db,binding['binding_id'],delivered['message_id'],
                                      generation_id=result.get('generation_id'),segment_index=index)
                    ids.append(str(delivered['message_id']))
            if ids:
                self.attention.mark_replied(str(binding['binding_id']),bot_message_ids=ids)
        return {'sent':bool(sent),'reason':'ok' if sent else 'empty','segments':sent}

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

    def _typing_delay(self, part, policy):
        """Log-shaped human-ish pause before a bubble.

        A fixed per-character delay makes long messages feel wrong; AstrBot's
        proactive-chat plugin uses a log-shaped interval for the same reason.
        Jitter avoids a metronomic cadence. ``reply_delay_seconds=0`` disables
        pacing completely (used by deterministic tests).
        """
        base=float(policy.reply_delay_seconds)
        if base<=0:
            return 0.0
        if part.get('type')=='text':
            length=len(str((part.get('data') or {}).get('text') or ''))
            delay=base+math.log1p(max(0,length))*1.05+random.uniform(0.15,0.55)
        else:
            delay=base+1.0+random.uniform(0.15,0.55)
        return min(float(policy.reply_max_delay_seconds),max(0.0,delay))

    async def _pause_before_send(self, part, policy, *, first=False):
        delay=self._typing_delay(part,policy)
        if delay<=0:
            return
        if first:
            delay=min(delay,2.5)
        gap=max(0.0,self._last_send_at+float(policy.reply_min_gap_seconds)-time.monotonic())
        if max(delay,gap)>0:
            await asyncio.sleep(max(delay,gap))

    def _mark_sent(self):
        self._last_send_at=time.monotonic()

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

    async def _media_part(self, item):
        kind=item.get('kind')
        if kind=='sticker': kind='image'  # sticker attachments are image assets sent as QQ images
        if kind not in ('image','audio'): return None
        try:
            asset,path=self.assets.get(self.actor,UUID(item['asset_id']))
        except Exception:
            return None
        if asset['kind']!=kind or asset['bytes']>10*1024*1024: return None
        raw=await asyncio.to_thread(path.read_bytes)
        return {'type':'record' if kind=='audio' else 'image',
                'data':{'file':'base64://'+base64.b64encode(raw).decode()}}

    async def _reply_parts(self, content):
        # A voiced segment must not be repeated as text: QQ receives the record,
        # the text bubble stays as the fallback for failed or missing speech.
        attachments=list(content.get('attachments') or [])
        segments=sorted(content.get('segments') or [],key=lambda item:item.get('index',0))
        parts=[]
        if segments:
            audio={str(item.get('segment_id')):item for item in attachments
                   if item.get('kind')=='audio' and item.get('segment_id')}
            for segment in segments:
                item=audio.get(str(segment.get('segment_id') or '')) if segment.get('kind')=='voice' else None
                part=await self._media_part(item) if item else None
                parts.append(part or {'type':'text','data':{'text':segment.get('text','')}})
            attachments=[item for item in attachments if item.get('kind')!='audio']
        else:
            parts=[{'type':'text','data':{'text':text}} for text,_ in split_reply(content.get('text',''))]
        for item in attachments:
            part=await self._media_part(item)
            if part is not None: parts.append(part)
        return parts


def install_qq_routes(app,settings,store,service,assets,actor,authenticate,wakeup):
    from ..core.settings import persist_settings,SETTINGS_WRITE_LOCK
    bridge=QQBridge(app,settings,store,service,assets,actor,wakeup)
    app.state.qq_bridge=bridge
    service.qq_bridge=bridge

    @app.get('/api/v1/integrations/qq')
    async def status(current=Depends(authenticate)):
        return bridge.view()

    @app.post('/api/v1/integrations/qq/outbox/send')
    async def send_outbox_file(body: QQOutboxFile,current=Depends(authenticate)):
        """Send one file from data/qq-outbox to the bound owner over QQ.

        The path is a filename, not an arbitrary path: the endpoint can only
        read from the dedicated outbox directory, so it cannot exfiltrate
        settings, tokens or other project data.
        """
        policy=settings.qq
        if not policy.enabled or not policy.owner_qq:
            raise AppError('QQ_DISABLED','QQ 私聊未启用',409)
        name=body.name
        if name != Path(name).name or name in ('.','..') or '/' in name or '\\' in name:
            raise AppError('QQ_OUTBOX_NAME','文件名无效',422)
        root=(Path(settings.data_dir)/'qq-outbox').resolve()
        path=(root/name).resolve()
        if root not in path.parents or not path.is_file():
            raise AppError('QQ_OUTBOX_MISSING','文件不在 QQ 发件箱中',404)
        if path.stat().st_size>25*1024*1024:
            raise AppError('QQ_OUTBOX_SIZE','文件超过 25MB',413)
        connection=bridge.connection
        if connection is None or not connection.connected:
            raise AppError('QQ_OFFLINE','QQ 当前未连接',409)
        result=await connection.call('upload_private_file',{
            'user_id':int(policy.owner_qq),
            'file':'file:///'+path.as_posix(),
            'name':path.name,
            'upload_file':True,
        })
        logger.info('qq outbox file sent bytes=%s',path.stat().st_size)
        return {'sent':True,'name':path.name,'file_id':(result or {}).get('file_id')}

    @app.get('/api/v1/integrations/qq/groups')
    def groups(companion_id:UUID,current=Depends(authenticate)):
        """Persistent group windows for the current companion (read-only listing)."""
        from ..identity.service import authorize as authorize_companion
        allowlist=set(settings.qq.allowed_groups)
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
                     'enabled':row['external_chat_id'] in allowlist,
                     'conversation_state':row['conversation_state'],
                     'created_at':row['created_at']} for row in rows]

    @app.patch('/api/v1/integrations/qq/groups/{binding_id}')
    async def toggle_group(binding_id:UUID,body:QQGroupToggle,current=Depends(authenticate)):
        """Per-group enable/disable: the allowlist is authoritative, the binding mirrors it."""
        from ..identity.service import authorize as authorize_companion
        with service.db.engine.connect() as conn:
            row=conn.execute(select(m.channel_bindings).where(
                m.channel_bindings.c.id==binding_id,
                m.channel_bindings.c.account_id==current.account_id)).mappings().first()
            if not row: raise AppError('NOT_FOUND','群窗口不存在',404)
            authorize_companion(conn,current,row['companion_id'])
            group_id=row['external_chat_id']
        async with bridge.connection_lock:
            with SETTINGS_WRITE_LOCK:
                previous=settings.qq
                try:
                    groups=list(previous.allowed_groups)
                    if body.enabled and group_id not in groups: groups.append(group_id)
                    if not body.enabled and group_id in groups: groups.remove(group_id)
                    policy=QQSettings.model_validate({**previous.model_dump(),'allowed_groups':groups})
                    settings.qq=policy
                    persist_settings(settings)
                except Exception:
                    settings.qq=previous
                    raise
            with service.db.engine.begin() as conn:
                conn.execute(m.channel_bindings.update().where(
                    m.channel_bindings.c.id==binding_id).values(enabled=body.enabled,updated_at=m.now()))
        logger.info('qq group %s enabled=%s',group_id,body.enabled)
        return {'binding_id':str(binding_id),'group_id':group_id,'enabled':body.enabled,
                'allowed_groups':groups}

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
        bridge.attention=GroupAttention(policy.followup_seconds,policy.followup_max_rounds,
            state_path=settings.data_dir/'qq-attention.json')
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
            # ASGI cancels the connection scope on disconnect. Finish owned
            # resource cleanup even when an await is hit inside that scope.
            with CancelScope(shield=True):
                connection.disconnect()
                if bridge.connection is connection: await bridge.stop()
                worker.cancel(); await asyncio.gather(worker,return_exceptions=True)
                if bridge.connection is connection: bridge.connection=None
                bridge.last_status='waiting' if settings.qq.enabled else 'disabled'
                logger.info('qq onebot disconnected')

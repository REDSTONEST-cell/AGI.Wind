import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agi_wind.integrations.qq import QQSettings, QQConnection, private_message, request_id_for


def config(**changes):
    return QQSettings(enabled=True, owner_qq='10001', bot_qq='10002',
                      conversation_id=uuid4(), **changes)


def event(**changes):
    return dict(post_type='message', message_type='private', sub_type='friend',
                self_id=10002, user_id=10001, message_id=8,
                message=[{'type':'text','data':{'text':'你好。'}}], **changes)


def test_only_bound_owner_friend_messages_are_accepted():
    settings=config()
    assert private_message(event(), settings)['text']=='你好。'
    for key,value in [('user_id',99999),('self_id',99999),('message_type','group'),
                      ('post_type','message_sent'),('sub_type','group')]:
        data=event(); data[key]=value
        assert private_message(data,settings) is None
    data=event(); data['message']=[{'type':'image','data':{'url':'https://example.com/image.png'}}]
    assert private_message(data,settings)['images']==['https://example.com/image.png']


def test_missing_ids_unknown_segments_and_disabled_settings_are_ignored():
    settings=config()
    data=event(); data.pop('message_id')
    assert private_message(data,settings) is None
    data=event(); data['message']='[CQ:image,file=local-secret]'
    assert private_message(data,settings) is None
    data=event(); data['message']=[{'type':'file','data':{'file':'C:/private'}}]
    assert private_message(data,settings) is None
    settings.enabled=False
    assert private_message(event(),settings) is None


def test_enabled_configuration_needs_explicit_binding():
    with pytest.raises(ValidationError): QQSettings(enabled=True)
    assert QQSettings().enabled is False
    assert request_id_for('10002',8)==request_id_for('10002',8)
    assert request_id_for('10002',8)!=request_id_for('99999',8)


async def test_echo_responses_do_not_block_behind_event_work():
    class Wire:
        async def send_json(self,data): self.sent=data
    wire=Wire(); connection=QQConnection(wire)
    pending=asyncio.create_task(connection.call('send_private_msg',{'user_id':12345}))
    await asyncio.sleep(0)
    connection.resolve({'status':'ok','retcode':0,'data':{'message_id':20},'echo':wire.sent['echo']})
    assert (await pending)['message_id']==20
    assert not connection.pending


async def test_failed_echo_and_disconnect_never_retry():
    class Wire:
        calls=0
        async def send_json(self,data): self.sent=data; self.calls+=1
    wire=Wire(); connection=QQConnection(wire)
    pending=asyncio.create_task(connection.call('send_private_msg',{}))
    await asyncio.sleep(0)
    connection.resolve({'status':'failed','retcode':1200,'echo':wire.sent['echo']})
    with pytest.raises(RuntimeError): await pending
    assert wire.calls==1
    pending=asyncio.create_task(connection.call('send_private_msg',{}))
    await asyncio.sleep(0)
    connection.disconnect()
    with pytest.raises(ConnectionError): await pending
    assert not connection.pending


async def test_image_download_rejects_local_urls_before_network():
    from agi_wind.integrations.qq import download_image
    from agi_wind.core.errors import AppError
    for url in ('file:///C:/private','https://127.0.0.1/a','https://qpic.cn.evil.invalid/a',
                'http://gchat.qpic.cn:8080/a','https://user:password@gchat.qpic.cn/a'):
        with pytest.raises(AppError): await download_image(url,100)


async def test_stop_cancels_busy_job_and_clears_queued_messages():
    from agi_wind.integrations.qq import QQBridge
    from types import SimpleNamespace
    class Service:
        async def interrupt(self,actor,gid,command): self.stopped=gid
    service=Service(); bridge=QQBridge(None,SimpleNamespace(qq=config()),None,service,None,None,None)
    bridge.job=asyncio.create_task(asyncio.sleep(60))
    gid=uuid4(); bridge.generation_id=gid
    bridge.queue.put_nowait({'synthetic':True})
    await bridge.stop()
    assert service.stopped==gid and bridge.job.cancelled() and bridge.queue.empty()


async def test_stop_never_starts_next_queued_job_while_interrupt_awaits():
    from agi_wind.integrations.qq import QQBridge
    from types import SimpleNamespace
    class Service:
        async def interrupt(self,*args): await asyncio.sleep(0.01)
    class Wire:
        async def close(self,**kwargs): pass
    connection=QQConnection(Wire())
    bridge=QQBridge(None,SimpleNamespace(qq=config()),None,Service(),None,None,None)
    bridge.connection=connection
    started=[]
    async def respond(conn,msg):
        started.append(msg)
        bridge.generation_id=uuid4()
        await asyncio.sleep(60)
    bridge.respond=respond
    bridge.queue.put_nowait('first'); bridge.queue.put_nowait('queued')
    consumer=asyncio.create_task(bridge.consume(connection))
    await asyncio.sleep(0); await asyncio.sleep(0)
    await bridge.stop(); await asyncio.sleep(0)
    assert started==['first'] and bridge.queue.empty()
    consumer.cancel(); await asyncio.gather(consumer,return_exceptions=True)


async def test_scoped_approval_revoke_keeps_other_web_task_pending():
    from agi_wind.agent.permissions import ApprovalQueue
    from agi_wind.core.types import Actor
    from agi_wind.core.errors import AppError
    actor=Actor(account_id=uuid4(),user_id=uuid4())
    queue=ApprovalQueue(); qq_id,web_id=uuid4(),uuid4()
    qq=asyncio.create_task(queue.request(actor,qq_id,'read_file',{}))
    web=asyncio.create_task(queue.request(actor,web_id,'read_file',{}))
    await asyncio.sleep(0)
    try:
        queue.revoke(actor,generation_id=qq_id)
        with pytest.raises(AppError): await qq
        assert [r['generation_id'] for r in queue.list(actor)]==[str(web_id)]
    finally:
        qq.cancel(); web.cancel(); await asyncio.gather(qq,web,return_exceptions=True)

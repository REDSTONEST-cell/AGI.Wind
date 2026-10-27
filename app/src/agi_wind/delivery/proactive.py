"""Bounded, idle-time proactive messages.

The scheduler is intentionally conservative: globally disabled by default, quiet
hours, minimum idle time, a minimum gap, a daily cap, one in-flight send per
companion, and no proactive message while a chat generation is running.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ..archive import models as m
from ..archive.repository import lock, digest
from ..archive.service import ArchiveService
from ..core.types import Actor
from ..core.errors import AppError
from . import models as dm

logger = logging.getLogger("agi_wind.proactive")

def parse_clock(value: str):
    hour, minute = str(value or "").split(":")
    return int(hour), int(minute)


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """Quiet range is local wall-clock; equal start/end means no quiet window."""
    start_h, start_m = parse_clock(start)
    end_h, end_m = parse_clock(end)
    if (start_h, start_m) == (end_h, end_m):
        return False
    minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    if start_minutes < end_minutes:
        return start_minutes <= minutes < end_minutes
    return minutes >= start_minutes or minutes < end_minutes


def countable_message():
    """Old schedule rows in group rooms were misroutes, not legitimate sends."""
    return (dm.proactive_messages.c.trigger != 'schedule') | dm.proactive_messages.c.conversation_id.in_(
        select(m.conversations.c.id).where(m.conversations.c.channel_kind=='web'))


class ProactiveService:
    def __init__(self, db, settings, service, archive=None, now_fn=None):
        self.db = db
        self.settings = settings
        self.service = service
        self.archive = archive or ArchiveService(db)
        self._now_fn = now_fn
        self._inflight: set = set()
        self._active = {}
        self._paused = {}
        # Set by the gateway; lets a delivered proactive message cancel any pending
        # after-reply cognition for the same conversation.
        self.idle_cognition = None

    def _user_marker(self, actor):
        with self.db.engine.connect() as conn:
            return conn.scalar(select(m.events.c.id).join(m.conversations).where(
                m.conversations.c.account_id==actor.account_id, m.conversations.c.user_id==actor.user_id,
                m.events.c.role=='user',m.events.c.visibility=='visible').order_by(
                m.events.c.recorded_at.desc(),m.events.c.id.desc()).limit(1))

    def cancel_actor(self, actor):
        key=(actor.account_id,actor.user_id)
        self._paused[key]=self._user_marker(actor)
        tasks=[task for owner,task in self._active.values() if owner==key and not task.done()]
        for task in tasks:
            task.get_loop().call_soon_threadsafe(task.cancel)
        return len(tasks)

    def _latest(self, conn, conversation_id):
        return conn.execute(select(m.events).where(m.events.c.conversation_id==conversation_id,
            m.events.c.visibility=='visible').order_by(m.events.c.recorded_at.desc(),m.events.c.id.desc()).limit(1)).mappings().first()

    def now(self) -> datetime:
        if self._now_fn is not None:
            return self._now_fn()
        return datetime.now(ZoneInfo(self.settings.timezone))

    def conversation_for(self, actor, companion_id):
        with self.db.engine.connect() as conn:
            query = select(m.conversations)
            qq = getattr(self.settings, 'qq', None)
            if qq and qq.enabled and qq.conversation_id:
                query = query.where(m.conversations.c.id == qq.conversation_id)
            return conn.execute(
                query
                .where(
                    m.conversations.c.account_id == actor.account_id,
                    m.conversations.c.user_id == actor.user_id,
                    m.conversations.c.companion_id == companion_id,
                    m.conversations.c.state == "active",
                    m.conversations.c.channel_kind == "web",
                )
                .order_by(m.conversations.c.created_at.desc())
                .limit(1)
            ).mappings().first()

    def _last_user_at(self, actor, companion_id):
        conversation = self.conversation_for(actor, companion_id)
        if conversation is None:
            return None
        with self.db.engine.connect() as conn:
            return conn.scalar(
                select(func.max(m.events.c.occurred_at)).where(
                    m.events.c.account_id == actor.account_id,
                    m.events.c.companion_id == companion_id,
                    m.events.c.conversation_id == conversation['id'],
                    m.events.c.role == "user",
                    m.events.c.visibility == "visible",
                )
            )

    def _wake_stats(self, companion_id, now):
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.db.engine.connect() as conn:
            rows = dm.idle_cognition_runs
            condition = (rows.c.companion_id == companion_id, rows.c.reason == 'scheduled_wake')
            last = conn.scalar(select(func.max(rows.c.started_at)).where(*condition))
            today = conn.scalar(select(func.count()).select_from(rows).where(
                *condition, rows.c.started_at >= day, rows.c.started_at < day + timedelta(days=1))) or 0
        return last, int(today)

    def _last_sent_at(self, companion_id):
        with self.db.engine.connect() as conn:
            return conn.scalar(
                select(func.max(dm.proactive_messages.c.created_at)).where(
                    dm.proactive_messages.c.companion_id == companion_id, countable_message()
                )
            )

    def _sent_today(self, companion_id, day: datetime) -> int:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        with self.db.engine.connect() as conn:
            return int(
                conn.scalar(
                    select(func.count())
                    .select_from(dm.proactive_messages)
                    .where(
                        dm.proactive_messages.c.companion_id == companion_id,
                        countable_message(),
                        dm.proactive_messages.c.created_at >= start,
                        dm.proactive_messages.c.created_at < end,
                    )
                )
                or 0
            )

    def _generation_running(self, conversation_id) -> bool:
        with self.db.engine.connect() as conn:
            count = conn.scalar(
                select(func.count())
                .select_from(m.generations.join(m.requests, m.generations.c.request_id == m.requests.c.id))
                .where(
                    m.requests.c.conversation_id == conversation_id,
                    m.generations.c.state.in_(("pending", "running", "draining")),
                )
            )
        return bool(count)

    def due(self, actor, companion_id):
        policy = self.settings.proactive
        now = self.now()
        if not policy.enabled:
            return False, "disabled"
        key=(actor.account_id,actor.user_id)
        if key in self._paused:
            if self._paused[key] == self._user_marker(actor):
                return False, 'stopped'
            self._paused.pop(key,None)
        conversation = self.conversation_for(actor, companion_id)
        if conversation is None:
            return False, "no_active_conversation"
        with self.db.engine.connect() as conn:
            active=conn.scalar(select(dm.idle_cognition_runs.c.id).where(
                dm.idle_cognition_runs.c.conversation_id==conversation['id'],
                dm.idle_cognition_runs.c.state.in_(('pending','running')),
                dm.idle_cognition_runs.c.checkpoint['continuation'].as_boolean().is_(True)).limit(1))
        if active is not None:
            return False, 'agent_active'
        if self._generation_running(conversation["id"]):
            return False, "generation_running"
        if callable(getattr(self.service,'call_active',None)) and self.service.call_active(conversation['id']):
            return False, 'call_active'
        last_user = self._last_user_at(actor, companion_id)
        if last_user is None:
            last_user = conversation["created_at"]
        if last_user is not None and now - last_user < timedelta(minutes=policy.min_idle_minutes):
            return False, "not_idle"
        last_sent = self._last_sent_at(companion_id)
        if last_sent is not None and now - last_sent < timedelta(minutes=policy.min_gap_minutes):
            return False, "gap"
        if self._sent_today(companion_id, now) >= policy.max_per_day:
            return False, "daily_cap"
        last_wake, wakes = self._wake_stats(companion_id, now)
        if wakes >= policy.max_per_day:
            return False, 'wake_cap'
        if last_wake is not None and now - last_wake < timedelta(minutes=policy.min_gap_minutes):
            return False, 'wake_gap'
        if in_quiet_hours(now, policy.quiet_start, policy.quiet_end):
            return False, "quiet_hours"
        return True, "ok"

    def stats(self, actor, companion_id):
        now = self.now()
        last_sent = self._last_sent_at(companion_id)
        due, reason = self.due(actor, companion_id)
        last_wake, wakes = self._wake_stats(companion_id, now)
        conversation = self.conversation_for(actor, companion_id)
        delivery = None
        if conversation:
            with self.db.engine.connect() as conn:
                content = conn.scalar(select(m.events.c.content).where(
                    m.events.c.conversation_id==conversation['id'],m.events.c.visibility=='visible',
                    m.events.c.content['proactive'].as_boolean().is_(True)).order_by(
                    m.events.c.recorded_at.desc(),m.events.c.id.desc()).limit(1))
                delivery = (content or {}).get('qq_delivery')
        return {
            "last_sent_at": last_sent.isoformat() if last_sent else None,
            "sent_today": self._sent_today(companion_id, now),
            "due": due,
            "reason": reason,
            "last_wake_at": last_wake.isoformat() if last_wake else None,
            "wakes_today": wakes,
            "last_qq_delivery": delivery,
        }

    async def deliver(self, actor, conversation, text, *, trigger="schedule", model_ref=None, cancel_pending=True, precondition=None, media=None, target=None, delivery_check=None):
        """Archive and record one already-generated proactive message.

        Idle cognition uses this after its own bounded thinking so the model is not
        called a second time just to write the message.
        """
        text = str(text or "").strip()
        if trigger in ('schedule', 'manual') and conversation['channel_kind'] != 'web':
            return {'sent':False, 'reason':'owner_private_only'}
        if not text:
            return {"sent": False, "reason": "empty"}
        target=target or {'kind':'web'}
        mirror=False
        sync={}
        qq=getattr(self.settings,'qq',None)
        if (target['kind']=='web' and qq and qq.enabled and self.settings.proactive.after_reply_qq_enabled
                and str(qq.conversation_id)==str(conversation['id'])):
            mirror=True
            target={'kind':'private','bot':qq.bot_qq,'recipient':qq.owner_qq,
                    'conversation_id':str(conversation['id'])}
        bridge=getattr(self.service,'qq_bridge',None)
        if target['kind']!='web' and (bridge is None or not bridge.proactive_available(target)):
            if not mirror:
                return {'sent':False,'reason':'channel_unavailable'}
            target={'kind':'web'}
            sync={'qq_synced':False,'qq_reason':'channel_unavailable'}
        def validate(conn, current):
            if trigger in ('schedule', 'manual') and current['channel_kind'] != 'web':
                raise AppError('OWNER_PRIVATE_ONLY', '定时唤醒只发送到主人私聊', 409)
            if callable(getattr(self.service,'call_active',None)) and self.service.call_active(current['id']):
                raise AppError('CALL_ACTIVE','通话期间暂停主动追话',409)
            if trigger!='manual':
                policy=self.settings.proactive
                local=self.now().astimezone(ZoneInfo(self.settings.timezone))
                day=local.replace(hour=0,minute=0,second=0,microsecond=0)
                used=conn.scalar(select(func.count()).select_from(dm.proactive_messages).where(
                    dm.proactive_messages.c.companion_id==current['companion_id'],
                    countable_message(),
                    dm.proactive_messages.c.created_at>=day,dm.proactive_messages.c.created_at<day+timedelta(days=1))) or 0
                if not policy.enabled or used>=policy.max_per_day or in_quiet_hours(local,policy.quiet_start,policy.quiet_end):
                    raise AppError('PROACTIVE_CANCELLED','主动追话设置或额度已变化',409)
            if target['kind']!='web' and not mirror and not bridge.proactive_available(target):
                raise AppError('QQ_UNAVAILABLE','QQ接入已变化',409)
            if precondition:
                precondition(conn,current)
        device_id='proactive' if target['kind']=='web' or mirror else f"qq-{target['kind']}:{target['bot']}:{target['recipient']}"
        result = self.archive.create_proactive_message(
            actor,
            conversation["id"],
            text,
            model_ref=model_ref,
            runtime="proactive",
            trigger=trigger,
            precondition=validate,
            media=media,
            device_id=device_id,
        )
        if target['kind']!='web':
            async def check():
                if delivery_check:
                    await delivery_check()
                with self.db.engine.connect() as conn:
                    latest=self._latest(conn,conversation['id'])
                    companion=conn.execute(select(m.companions).where(m.companions.c.id==conversation['companion_id'])).mappings().one()
                    generated=conn.execute(select(m.generations).where(m.generations.c.id==result['generation_id'])).mappings().one()
                if (not latest or latest['id']!=result['event_id'] or companion['memory_revision']!=generated['memory_revision']
                        or companion['active_persona_version']!=generated['persona_version']):
                    raise AppError('PROACTIVE_CANCELLED','主动追话上下文已变化',409)
            try:
                sent=await bridge.deliver_proactive(target,{'text':text,**(media or {})},result,check)
            except Exception as error:
                if not mirror:
                    raise
                logger.warning('proactive QQ sync failed type=%s',type(error).__name__)
                sent={'sent':False,'reason':'qq_sync_failed'}
            sync={'qq_synced':bool(sent is None or sent.get('sent'))}
            if sent and not sent.get('sent'):
                if not mirror:
                    return {**result,'sent':False,'reason':sent.get('reason','qq_failed')}
                sync['qq_reason']=sent.get('reason','qq_failed')
        if sync:
            # Archive presence is distinct from platform delivery. Keep a durable
            # outcome so an offline/failed mirror is never reported as QQ success.
            with self.db.engine.begin() as conn:
                lock(conn, conversation['companion_id'])
                content = conn.scalar(select(m.events.c.content).where(m.events.c.id==result['event_id']))
                if content is not None:
                    content = {**content, 'qq_delivery': sync}
                    conn.execute(m.events.update().where(m.events.c.id==result['event_id']).values(
                        content=content, content_sha256=digest(content)))
            if not sync.get('qq_synced'):
                logger.warning('proactive QQ not delivered conversation=%s reason=%s',conversation['id'],sync.get('qq_reason'))
        if cancel_pending and self.idle_cognition is not None:
            self.idle_cognition.cancel_conversation(conversation["id"], reason="proactive_sent")
        logger.info("proactive message sent companion=%s conversation=%s trigger=%s",
                    conversation["companion_id"], conversation["id"], trigger)
        return {"sent": True, "reason": "ok", **result, **sync}

    async def send(self, actor, companion_id, *, force=False, trigger="schedule"):
        if companion_id in self._inflight:
            return {'sent':False,'reason':'busy'}
        conversation=self.conversation_for(actor,companion_id)
        if conversation is None:
            return {'sent':False,'reason':'no_active_conversation'}
        if not force:
            due,reason=self.due(actor,companion_id)
            if not due:
                return {'sent':False,'reason':reason}
        if getattr(self.service,'provider',None) is None or getattr(self.service,'model',None) is None:
            return {'sent':False,'reason':'model_unconfigured'}
        if self.idle_cognition is None:
            from .idle_cognition import IdleCognitionService
            self.idle_cognition=IdleCognitionService(self.db,self.settings,self.service,
                                                    proactive=self,now_fn=self.now)
        self._inflight.add(companion_id)
        self._active[companion_id]=((actor.account_id,actor.user_id),asyncio.current_task())
        try:
            return await self.idle_cognition.start_wake(actor,conversation,force=force,trigger=trigger)
        finally:
            self._inflight.discard(companion_id)
            self._active.pop(companion_id,None)

    async def tick(self):
        if not self.settings.proactive.enabled:
            return 0
        with self.db.engine.connect() as conn:
            companions = conn.execute(select(m.companions.c.id, m.companions.c.account_id)).mappings().all()
        sent = 0
        for companion in companions:
            conversation = None
            try:
                with self.db.engine.connect() as conn:
                    conversation = conn.execute(
                        select(m.conversations)
                        .where(
                            m.conversations.c.account_id == companion["account_id"],
                            m.conversations.c.companion_id == companion["id"],
                            m.conversations.c.state == "active",
                            m.conversations.c.channel_kind == "web",
                        )
                        .order_by(m.conversations.c.created_at.desc())
                        .limit(1)
                    ).mappings().first()
                if conversation is None:
                    continue
                actor = Actor(account_id=companion["account_id"], user_id=conversation["user_id"])
                child=asyncio.create_task(self.send(actor, companion['id']))
                try:
                    result=await child
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    continue  # Stop cancels this send, not the scheduler itself.
                if result.get("sent"):
                    sent += 1
            except Exception:
                logger.warning("proactive tick failed companion=%s", companion["id"], exc_info=True)
        return sent

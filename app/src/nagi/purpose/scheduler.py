"""Background host for the purpose engine.

The engine and service stay pure. This adapter is the only place that connects a
purpose decision to the existing idle-cognition executor, so purpose openings are
delivered through the same cancellation, quiet-hours and archive path as wakes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from ..archive import models as m
from .service import PurposeDeps, PurposeService
from .store import DbPurposeStore

logger = logging.getLogger("nagi.purpose")


class PurposeScheduler:
    def __init__(self, db, settings, actor, idle_cognition, *, interval=60):
        self.db = db
        self.settings = settings
        self.actor = actor
        self.idle_cognition = idle_cognition
        self.interval = max(15, int(interval))
        self._closing = asyncio.Event()

    def stop(self):
        self._closing.set()

    def _conversation(self, companion_id: UUID):
        policy = self.settings.qq
        with self.db.engine.connect() as conn:
            if getattr(self.settings.purpose, "carrier", "qq") == "qq":
                if not (policy.enabled and policy.conversation_id):
                    return None
                row = conn.execute(
                    select(m.conversations).where(
                        m.conversations.c.id == policy.conversation_id,
                        m.conversations.c.account_id == self.actor.account_id,
                        m.conversations.c.companion_id == companion_id,
                    )
                ).mappings().first()
                return dict(row) if row else None
            row = conn.execute(
                select(m.conversations)
                .where(
                    m.conversations.c.account_id == self.actor.account_id,
                    m.conversations.c.companion_id == companion_id,
                    m.conversations.c.state == "active",
                    m.conversations.c.channel_kind == "web",
                )
                .order_by(m.conversations.c.created_at.desc())
                .limit(1)
            ).mappings().first()
            return dict(row) if row else None

    def _companions(self):
        with self.db.engine.connect() as conn:
            return [
                str(row[0])
                for row in conn.execute(
                    select(m.companions.c.id).where(
                        m.companions.c.account_id == self.actor.account_id
                    )
                )
            ]

    async def tick(self) -> int:
        if not self.settings.purpose.enabled:
            return 0
        sent = 0
        for companion_id in self._companions():
            conversation = self._conversation(UUID(companion_id))
            if conversation is None:
                continue
            service = PurposeService(
                PurposeDeps(
                    store=DbPurposeStore(
                        self.db.engine,
                        account_id=self.actor.account_id,
                        companion_id=UUID(companion_id),
                        conversation_id=conversation["id"],
                    ),
                    settings=self.settings.purpose,
                    timezone=self.settings.timezone,
                )
            )
            decision = service.tick()
            if decision is None:
                continue
            result = await self.idle_cognition.start_wake(
                self.actor,
                conversation,
                force=True,
                trigger="purpose",
                purpose=decision,
            )
            if result.get("sent"):
                service.confirm(decision)
                sent += 1
                logger.info("purpose delivered companion=%s purpose=%s", companion_id, decision.purpose)
            else:
                logger.info("purpose not delivered companion=%s reason=%s", companion_id, result.get("reason"))
        return sent

    async def run(self):
        while not self._closing.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("purpose tick failed", exc_info=True)
            try:
                await asyncio.wait_for(self._closing.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass


__all__ = ["PurposeScheduler"]

"""PostgreSQL-backed context adapter for the purpose engine.

The engine deliberately knows nothing about SQL. This adapter reads only a bounded
window (recent events, active memories, delivery counts) and owns the two small
operational rows in ``purpose_state`` / ``purpose_runs``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ..archive.models import events
from ..memory.models import memory_items
from .closure import ClosureRecord
from .models import purpose_runs, purpose_state


def _as_dict(value):
    return dict(value) if value is not None else None


class DbPurposeStore:
    def __init__(self, engine, *, account_id: UUID, companion_id: UUID, conversation_id: UUID | None = None):
        self.engine = engine
        self.account_id = account_id
        self.companion_id = companion_id
        self.conversation_id = conversation_id

    def _scope(self, table):
        query = select(table).where(
            table.c.account_id == self.account_id,
            table.c.companion_id == self.companion_id,
        )
        return query

    def last_user_at(self) -> datetime | None:
        query = select(func.max(events.c.occurred_at)).where(
            events.c.account_id == self.account_id,
            events.c.companion_id == self.companion_id,
            events.c.role == "user",
        )
        if self.conversation_id is not None:
            query = query.where(events.c.conversation_id == self.conversation_id)
        with self.engine.connect() as conn:
            return conn.scalar(query)

    def last_delivery_at(self) -> datetime | None:
        query = select(func.max(purpose_runs.c.created_at)).where(
            purpose_runs.c.account_id == self.account_id,
            purpose_runs.c.companion_id == self.companion_id,
            purpose_runs.c.delivered.is_(True),
        )
        with self.engine.connect() as conn:
            return conn.scalar(query)

    def sent_today(self, now: datetime) -> int:
        local = now.astimezone(timezone.utc)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        query = select(func.count()).select_from(purpose_runs).where(
            purpose_runs.c.account_id == self.account_id,
            purpose_runs.c.companion_id == self.companion_id,
            purpose_runs.c.delivered.is_(True),
            purpose_runs.c.created_at >= start,
        )
        with self.engine.connect() as conn:
            return int(conn.scalar(query) or 0)

    def recent_events(self, limit: int):
        query = self._scope(events).order_by(events.c.occurred_at.desc()).limit(max(0, limit))
        if self.conversation_id is not None:
            query = query.where(events.c.conversation_id == self.conversation_id)
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(query).mappings()]
        rows.reverse()
        return rows

    def memories(self, limit: int):
        query = (
            self._scope(memory_items)
            .where(memory_items.c.status == "active")
            .order_by(memory_items.c.updated_at.desc().nullslast(), memory_items.c.created_at.desc())
            .limit(max(0, limit))
        )
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(query).mappings()]
        rows.reverse()
        return rows

    def _state(self):
        with self.engine.connect() as conn:
            return conn.execute(
                select(purpose_state).where(
                    purpose_state.c.account_id == self.account_id,
                    purpose_state.c.companion_id == self.companion_id,
                )
            ).mappings().first()

    def last_closure(self, now: datetime):
        row = self._state()
        return None if row is None else row["closure"]

    def save_closure(self, record: ClosureRecord) -> None:
        payload = record.model_dump(mode="json")
        with self.engine.begin() as conn:
            stmt = insert(purpose_state).values(
                account_id=self.account_id,
                companion_id=self.companion_id,
                closure=payload,
                updated_at=datetime.now(timezone.utc),
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[purpose_state.c.account_id, purpose_state.c.companion_id],
                    set_={"closure": payload, "updated_at": datetime.now(timezone.utc)},
                )
            )

    def clear_closure(self) -> None:
        with self.engine.begin() as conn:
            stmt = insert(purpose_state).values(
                account_id=self.account_id,
                companion_id=self.companion_id,
                closure=None,
                updated_at=datetime.now(timezone.utc),
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[purpose_state.c.account_id, purpose_state.c.companion_id],
                    set_={"closure": None, "updated_at": datetime.now(timezone.utc)},
                )
            )

    def record_decision(self, decision) -> None:
        payload = decision.model_dump(mode="json")
        with self.engine.begin() as conn:
            conn.execute(
                purpose_runs.insert().values(
                    account_id=self.account_id,
                    companion_id=self.companion_id,
                    conversation_id=self.conversation_id,
                    decision=payload,
                    delivered=False,
                )
            )
            stmt = insert(purpose_state).values(
                account_id=self.account_id,
                companion_id=self.companion_id,
                last_decision=payload,
                updated_at=datetime.now(timezone.utc),
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[purpose_state.c.account_id, purpose_state.c.companion_id],
                    set_={"last_decision": payload, "updated_at": datetime.now(timezone.utc)},
                )
            )

    def mark_latest_delivered(self) -> None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(purpose_runs.c.id).where(
                    purpose_runs.c.account_id == self.account_id,
                    purpose_runs.c.companion_id == self.companion_id,
                ).order_by(purpose_runs.c.created_at.desc()).limit(1)
            ).first()
            if row is not None:
                conn.execute(
                    purpose_runs.update()
                    .where(purpose_runs.c.id == row[0])
                    .values(delivered=True)
                )

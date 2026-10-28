"""Quota/usage HTTP surface installed by create_app."""
from __future__ import annotations

from fastapi import Depends
from pydantic import Field

from ..core.settings import Settings, persist_settings
from ..core.types import DTO
from ..usage.service import CATEGORIES, QuotaService
from ..usage.settings import QuotaSettings


class QuotaPatch(DTO):
    enabled: bool | None = None
    daily_total_tokens: int | None = Field(default=None, ge=0)
    daily_web_tokens: int | None = Field(default=None, ge=0)
    daily_qq_tokens: int | None = Field(default=None, ge=0)
    daily_memory_tokens: int | None = Field(default=None, ge=0)
    daily_cost_cny: float | None = Field(default=None, ge=0)
    warning_percent: int | None = Field(default=None, ge=0, le=100)
    hard_stop: bool | None = None


def install_usage_routes(app, settings: Settings, db, authenticate):
    def quota_for(current):
        return QuotaService(settings.usage, account_id=current.account_id)

    @app.get("/api/v1/usage/quota")
    def usage_quota(current=Depends(authenticate)):
        with db.engine.connect() as conn:
            service = quota_for(current)
            snapshot = service.summary(conn, current.account_id)
            checks = {category: service.check(category, conn).model_dump() for category in CATEGORIES}
        return {**snapshot, "checks": checks, "settings": settings.usage.model_dump(mode="json")}

    @app.get("/api/v1/usage/ledger")
    def usage_ledger_rows(limit: int = 50, current=Depends(authenticate)):
        limit = max(1, min(int(limit), 200))
        with db.engine.connect() as conn:
            from sqlalchemy import select
            from ..usage.models import usage_ledger

            rows = conn.execute(
                select(usage_ledger)
                .where(usage_ledger.c.account_id == current.account_id)
                .order_by(usage_ledger.c.created_at.desc())
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    @app.patch("/api/v1/usage/settings")
    def usage_settings_update(body: QuotaPatch, current=Depends(authenticate)):
        values = settings.usage.model_dump()
        values.update(body.model_dump(exclude_none=True))
        settings.usage = QuotaSettings.model_validate(values)
        persist_settings(settings)
        return {"settings": settings.usage.model_dump(mode="json")}

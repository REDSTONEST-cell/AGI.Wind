"""Purpose/closure HTTP surface.

Installed from ``create_app`` so the router stays independent of the large app module.
Every request is scoped to the authenticated owner and one companion; group callers never
reach this surface.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends
from pydantic import Field

from ..core.settings import Settings, persist_settings
from ..core.types import DTO
from ..purpose.service import PurposeDeps, PurposeService
from ..purpose.settings import PurposeSettings
from ..purpose.store import DbPurposeStore


class PurposePatch(DTO):
    enabled: bool | None = None
    carrier: str | None = None
    min_idle_minutes: int | None = Field(default=None, ge=1, le=1440)
    min_gap_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_per_day: int | None = Field(default=None, ge=1, le=20)
    quiet_start: str | None = None
    quiet_end: str | None = None
    closure_enabled: bool | None = None
    closure_ttl_hours: int | None = Field(default=None, ge=1, le=720)


def install_purpose_routes(app, settings: Settings, db, authenticate, authorize):
    def service_for(current, companion_id: UUID, conversation_id: UUID | None):
        with db.engine.connect() as conn:
            authorize(conn, current, companion_id)
        store = DbPurposeStore(
            db.engine,
            account_id=current.account_id,
            companion_id=companion_id,
            conversation_id=conversation_id,
        )
        return PurposeService(
            PurposeDeps(store=store, settings=settings, timezone=settings.timezone)
        )

    def view(service: PurposeService):
        check = service.check()
        return {
            "allowed": check.allowed,
            "reason": check.reason,
            "decision": None if check.decision is None else check.decision.model_dump(mode="json"),
            "last_user_at": check.last_user_at,
            "last_delivery_at": check.last_delivery_at,
            "sent_today": check.sent_today,
            "has_closure": check.has_closure,
            "settings": settings.purpose.model_dump(mode="json"),
        }

    @app.get("/api/v1/purpose/status")
    def purpose_status(
        companion_id: UUID,
        conversation_id: UUID | None = None,
        current=Depends(authenticate),
    ):
        return view(service_for(current, companion_id, conversation_id))

    @app.get("/api/v1/purpose/preview")
    def purpose_preview(
        companion_id: UUID,
        conversation_id: UUID | None = None,
        current=Depends(authenticate),
    ):
        service = service_for(current, companion_id, conversation_id)
        decision = service.preview()
        return {
            "decision": None if decision is None else decision.model_dump(mode="json"),
            "settings": settings.purpose.model_dump(mode="json"),
        }

    @app.post("/api/v1/purpose/tick")
    def purpose_tick(
        companion_id: UUID,
        conversation_id: UUID | None = None,
        current=Depends(authenticate),
    ):
        service = service_for(current, companion_id, conversation_id)
        decision = service.tick()
        return {
            "decision": None if decision is None else decision.model_dump(mode="json"),
            "settings": settings.purpose.model_dump(mode="json"),
        }

    @app.post("/api/v1/purpose/closure/clear")
    def purpose_clear_closure(
        companion_id: UUID,
        conversation_id: UUID | None = None,
        current=Depends(authenticate),
    ):
        service = service_for(current, companion_id, conversation_id)
        service.clear_closure()
        return {"cleared": True}

    @app.patch("/api/v1/purpose/settings")
    def purpose_settings_update(body: PurposePatch, current=Depends(authenticate)):
        values = settings.purpose.model_dump()
        values.update(body.model_dump(exclude_none=True))
        settings.purpose = PurposeSettings.model_validate(values)
        persist_settings(settings)
        return {"settings": settings.purpose.model_dump(mode="json")}

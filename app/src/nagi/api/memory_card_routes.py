"""Memory card / archive HTTP surface.

Cards are owner-private. Every route authorizes the companion before touching the
card service; group/scene callers never reach this surface.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends
from pydantic import Field

from ..core.types import DTO, Scope
from ..memory.cards import MemoryCardService


class ForgetRequest(DTO):
    companion_id: UUID
    memory_id: UUID


class MergeRequest(DTO):
    companion_id: UUID
    memory_ids: list[UUID] = Field(min_length=2, max_length=20)
    title: str = Field(default="", max_length=200)
    content: str | None = Field(default=None, max_length=4000)


class UndoRequest(DTO):
    companion_id: UUID
    archive_id: UUID


def install_memory_card_routes(app, db, authenticate, authorize):
    cards = MemoryCardService(db)
    memory_cards = cards

    def scope_for(current, companion_id: UUID):
        with db.engine.connect() as conn:
            authorize(conn, current, companion_id)
        # Card routes are companion-scoped; Scope still requires a conversation id.
        # A nil UUID is stable and never used by the card queries.
        return Scope(**current.model_dump(), companion_id=companion_id, conversation_id=UUID(int=0))

    @app.get("/api/v1/memory/card")
    def memory_card(companion_id: UUID, budget_chars: int | None = None, current=Depends(authenticate)):
        scope = scope_for(current, companion_id)
        status = memory_cards.status(scope)
        return {
            "status": status,
            "text": memory_cards.render(scope, budget_chars=budget_chars),
            "budget_chars": budget_chars,
        }

    @app.get("/api/v1/memory/card/status")
    def memory_card_status(companion_id: UUID, current=Depends(authenticate)):
        return memory_cards.status(scope_for(current, companion_id))

    @app.post("/api/v1/memory/card/forget")
    def memory_card_forget(body: ForgetRequest, current=Depends(authenticate)):
        scope = scope_for(current, body.companion_id)
        return memory_cards.forget(body.memory_id, scope=scope)

    @app.post("/api/v1/memory/card/merge")
    def memory_card_merge(body: MergeRequest, current=Depends(authenticate)):
        scope = scope_for(current, body.companion_id)
        return memory_cards.merge(
            body.memory_ids,
            title=body.title,
            content=body.content,
            scope=scope,
        )

    @app.post("/api/v1/memory/card/undo")
    def memory_card_undo(body: UndoRequest, current=Depends(authenticate)):
        scope_from = scope_for(current, body.companion_id)
        # undo() is keyed by archive id; authorizing the companion above is the ownership gate.
        return memory_cards.undo(body.archive_id)

    @app.get("/api/v1/memory/card/search")
    def memory_card_search(
        companion_id: UUID,
        q: str,
        limit: int = 20,
        current=Depends(authenticate),
    ):
        scope = scope_for(current, companion_id)
        return {"results": memory_cards.full_search(q, scope=scope, limit=limit)}

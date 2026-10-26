from uuid import uuid4
import pytest
from agi_wind.core.errors import AppError
from agi_wind.core.types import Actor, ModelRef


async def test_identity_survives_model_switch_and_persona_history(db, actor, seeded_scope):
    from agi_wind.identity.service import IdentityService
    identity = IdentityService(db)
    initial = await identity.snapshot(actor, seeded_scope.companion_id)
    updated = await identity.update(actor, initial.companion_id, 1, {"core": {"name": "风"}, "style": {"tone": "温柔"}})
    await identity.bind_model(actor, initial.companion_id, ModelRef(provider_id="vendor-b", model_id="test"))
    current = await identity.snapshot(actor, initial.companion_id)
    assert current.companion_id == initial.companion_id
    assert current.version == 2 and current.core["name"] == "风"
    assert len(db.all("persona_versions")) == 2
    assert initial.core["development_only"] is True
    with pytest.raises(AppError) as error:
        await identity.update(actor, initial.companion_id, 1, {"core": {"name": "过期"}})
    assert error.value.status == 409


async def test_proposals_cannot_overwrite_core_or_cross_account(db, actor, seeded_scope):
    from agi_wind.identity.service import IdentityService
    identity = IdentityService(db)
    with pytest.raises(AppError):
        await identity.update(actor, seeded_scope.companion_id, 1, {"core": {"name": "自动覆盖"}}, origin="assistant")
    with pytest.raises(AppError) as error:
        await identity.snapshot(Actor(account_id=uuid4(), user_id=uuid4()), seeded_scope.companion_id)
    assert error.value.status == 403


async def test_relationship_and_commitments_shared_across_conversations(db, actor, seeded_scope):
    from agi_wind.identity.service import IdentityService
    from agi_wind.identity.models import relationships, commitments
    from agi_wind.archive.models import conversations
    with db.engine.begin() as conn:
        conn.execute(relationships.insert().values(companion_id=seeded_scope.companion_id, user_id=actor.user_id, data={"addressing": "小风"}))
        conn.execute(commitments.insert().values(companion_id=seeded_scope.companion_id, user_id=actor.user_id, content="周末一起看海", status="pending"))
        conn.execute(conversations.insert().values(id=uuid4(), account_id=actor.account_id, companion_id=seeded_scope.companion_id, user_id=actor.user_id))
    snapshot = await IdentityService(db).snapshot(actor, seeded_scope.companion_id)
    assert snapshot.relationship["addressing"] == "小风"
    assert snapshot.commitments[0]["content"] == "周末一起看海"

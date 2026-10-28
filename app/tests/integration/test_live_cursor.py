import httpx
from nagi.delivery.stream import emit_live
from test_client import client_app


async def test_live_cursor_is_authorized_readonly_snapshot(db,actor,seeded_scope,tmp_path):
    app=client_app(db,actor,tmp_path)
    with db.engine.begin() as conn:
        emit_live(conn,seeded_scope.conversation_id,'idle_cognition.completed',{'reason':'daily_cap'})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                                 headers={'Authorization':'Bearer synthetic-private-token'}) as client:
        path=f'/api/v1/conversations/{seeded_scope.conversation_id}/live-cursor'
        response=await client.get(path)
        assert response.status_code==200 and response.json()=={'sequence':1}
        with db.engine.begin() as conn:
            emit_live(conn,seeded_scope.conversation_id,'idle_cognition.started',{})
        assert (await client.get(path)).json()=={'sequence':2}
        from nagi.core.types import Actor
        from uuid import uuid4
        other=db.seed(Actor(account_id=uuid4(),user_id=uuid4()))
        denied=await client.get(f'/api/v1/conversations/{other.conversation_id}/live-cursor')
        assert denied.status_code in (403,404)
    assert db.count('conversation_live_events')==2

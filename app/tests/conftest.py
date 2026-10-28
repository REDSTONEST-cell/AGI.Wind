import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture
def actor():
    from nagi.core.types import Actor
    return Actor(account_id=uuid4(), user_id=uuid4())


@pytest.fixture
def chat_request_factory():
    from nagi.core.types import ChatRequest
    def factory(text="合成测试", conversation_id=None, **kw):
        return ChatRequest(request_id=kw.pop("request_id", uuid4()), conversation_id=conversation_id or uuid4(), input={"text": text}, device_id="test", **kw)
    return factory


@pytest.fixture
def fake_clock():
    return lambda: datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    from alembic import command
    from alembic.config import Config
    from nagi.archive.repository import Database
    root = Path(__file__).resolve().parents[2]
    url = os.environ.get("AGI_TEST_DATABASE_URL")
    if not url:
        config = root / "data/dev-db.json"
        if not config.exists():
            pytest.fail("Real PostgreSQL required: run scripts/setup-dev-postgres.py or set AGI_TEST_DATABASE_URL")
        url = json.loads(config.read_text(encoding="utf-8"))["url"]
    if make_url(url).get_backend_name() != "postgresql":
        pytest.fail("Integration tests require real PostgreSQL, never SQLite")
    schema = "test_" + uuid4().hex
    admin = create_engine(url, hide_parameters=True)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema}, hide_parameters=True)
    try:
        cfg = Config(str(root / "app/alembic.ini"))
        cfg.set_main_option("script_location", str(root / "app/migrations"))
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        yield Database(engine)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def seeded_scope(db, actor):
    return db.seed(actor)


@pytest.fixture
def archive(db):
    from nagi.archive.service import ArchiveService
    return ArchiveService(db)

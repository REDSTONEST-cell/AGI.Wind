"""Explicit real-CPU smoke; PostgreSQL rows live only in a disposable test schema."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_memory_semantic import fact
from agi_wind.memory.retrieval import MemoryService

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("AGI_MEMORY_MODEL_SMOKE") != "1",
    reason="explicit AGI_MEMORY_MODEL_SMOKE=1 required for real CPU model I/O")]


async def test_real_cpu_chinese_paraphrase_in_temporary_postgres(db, seeded_scope):
    from agi_wind.memory.semantic import index_semantic, semantic_status
    root = Path(__file__).resolve().parents[3]
    db.memory_settings = SimpleNamespace(data_dir=root/"data",memory_semantic_enabled=True)
    coffee, _ = fact(db,seeded_scope,"我最爱喝咖啡，每天早上喝一杯。")
    fact(db,seeded_scope,"我周末喜欢去海边看海浪。")
    fact(db,seeded_scope,"这台路由器支持无线网络。")
    await MemoryService(db).index_events(seeded_scope)
    assert (await index_semantic(db,seeded_scope))["indexed"] == 6
    hits = await MemoryService(db).search(seeded_scope,"我平时偏爱哪种提神饮品？")
    assert hits[0].memory_id == coffee
    assert semantic_status(db) == "semantic_local"

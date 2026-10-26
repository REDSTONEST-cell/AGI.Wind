from types import SimpleNamespace
from uuid import uuid4

from agi_wind.agent import tools as tools_module
from agi_wind.agent.tools import StationTools, StickerQuery


class FakeStickerService:
    rows = [
        {"id": uuid4(), "name": "收到敬礼", "description": "", "tags": ["收到", "敬礼", "元气"], "created_at": 1},
        {"id": uuid4(), "name": "开心蹦跳", "description": "", "tags": ["开心", "得意", "害羞"], "created_at": 2},
        {"id": uuid4(), "name": "困困摸鱼", "description": "", "tags": ["摸鱼", "困倦"], "created_at": 3},
    ]

    def __init__(self, db):
        pass

    def list(self, actor, companion_id, *, active_only=False):
        return list(self.rows)


def make_tools(monkeypatch):
    monkeypatch.setattr(tools_module, "StickerService", FakeStickerService)
    settings = lambda: SimpleNamespace(enabled=True, sticker_search_enabled=True)
    scope = SimpleNamespace(companion_id=uuid4())
    return StationTools(db=None, scope=scope, settings=settings, search=None, send=None)


async def test_sticker_search_scores_tags_and_bigrams(monkeypatch):
    tools = make_tools(monkeypatch)
    result = await tools.stickers(StickerQuery(query="得意"))
    assert [item["name"] for item in result["items"]] == ["开心蹦跳"]
    assert tools.selected_stickers == {FakeStickerService.rows[1]["id"]}


async def test_sticker_search_matches_any_token(monkeypatch):
    tools = make_tools(monkeypatch)
    result = await tools.stickers(StickerQuery(query="元气 收到"))
    assert result["items"][0]["name"] == "收到敬礼"


async def test_sticker_search_empty_result_returns_hint(monkeypatch):
    tools = make_tools(monkeypatch)
    result = await tools.stickers(StickerQuery(query="完全不存在"))
    assert result["items"] == []
    assert "空 query" in result["hint"]


async def test_sticker_search_empty_query_lists_recent(monkeypatch):
    tools = make_tools(monkeypatch)
    result = await tools.stickers(StickerQuery(query=""))
    assert len(result["items"]) == 3
    assert "hint" not in result

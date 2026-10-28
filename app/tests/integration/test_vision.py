from io import BytesIO
import base64
import httpx
import pytest
from PIL import Image
from nagi.core.types import ModelRef

async def test_image_payload_and_observation_are_archived(db, actor, seeded_scope, tmp_path):
    from nagi.archive.assets import AssetStore
    from nagi.providers.vision import VisionAdapter
    stream = BytesIO()
    Image.new("RGB", (3, 3), "blue").save(stream, "PNG")
    raw = stream.getvalue()
    store = AssetStore(db, tmp_path)
    asset = await store.put(actor, BytesIO(raw), "image/png")
    captured = []
    async def observe(model, messages):
        captured.extend(messages)
        return "合成测试：蓝色方块"
    adapter = VisionAdapter(db, store, observe, ModelRef(provider_id="fake",model_id="synthetic-vision"))
    result = await adapter.observe(seeded_scope, [asset["id"]], "什么颜色？")
    assert base64.b64encode(raw).decode() in str(captured)
    assert result["description"] == "合成测试：蓝色方块"
    assert db.all("asset_refs")[0]["asset_id"] == asset["id"]
    assert db.all("events")[0]["kind"] == "vision_observation"

async def test_vision_timeout_never_fabricates_description(db, actor, seeded_scope, tmp_path):
    from nagi.archive.assets import AssetStore
    from nagi.providers.vision import VisionAdapter
    async def timeout(model, messages): raise httpx.ReadTimeout("private")
    adapter = VisionAdapter(db, AssetStore(db, tmp_path), timeout, ModelRef(provider_id="fake",model_id="test"))
    with pytest.raises(Exception): await adapter.observe(seeded_scope, [], "看图")
    assert db.all("events", kind="vision_observation") == []

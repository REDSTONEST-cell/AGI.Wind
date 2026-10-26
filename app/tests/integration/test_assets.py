from io import BytesIO
from uuid import uuid4
from PIL import Image
import pytest
from agi_wind.core.errors import AppError


def image():
    stream = BytesIO()
    Image.new("RGB", (4, 4), "blue").save(stream, "PNG")
    return stream.getvalue()


def jpeg_image():
    stream = BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, "JPEG")
    return stream.getvalue()


async def test_mislabeled_extension_is_stored_as_its_decoded_format(db, actor, seeded_scope, tmp_path):
    """Drag-and-drop temp files often hold JPEG/WebP bytes under a .png name."""
    from agi_wind.archive.assets import AssetStore
    store = AssetStore(db, tmp_path)
    asset = await store.put(actor, BytesIO(jpeg_image()), "image/png", original_filename="TempDragFile(1).png")
    assert asset["mime"] == "image/jpeg"
    assert asset["relative_path"].endswith(".jpg")
    assert asset["metadata"]["original_filename"] == "TempDragFile(1).png"
    stored, path = store.get(actor, asset["id"])
    with Image.open(path) as opened:
        assert opened.format == "JPEG"


async def test_asset_dedup_does_not_grant_cross_account_access(db, actor, seeded_scope, tmp_path):
    from agi_wind.archive.assets import AssetStore
    from agi_wind.core.types import Actor
    store = AssetStore(db, tmp_path)
    first = await store.put(actor, BytesIO(image()), "image/png")
    again = await store.put(actor, BytesIO(image()), "image/png")
    assert first["id"] == again["id"]
    assert db.count("assets") == 1
    with pytest.raises(AppError) as error:
        store.get(Actor(account_id=uuid4(), user_id=uuid4()), first["id"])
    assert error.value.status == 403


async def test_invalid_truncated_and_oversize_uploads_never_ready(db, actor, seeded_scope, tmp_path):
    from agi_wind.archive.assets import AssetStore
    store = AssetStore(db, tmp_path, max_bytes=120)
    for data in (b"not an image", image()[:20], b"x" * 121):
        with pytest.raises(AppError):
            await store.put(actor, BytesIO(data), "image/png")
    assert db.count("assets") == 0
    assert not list(tmp_path.rglob("*.tmp"))


async def test_missing_or_other_account_attachment_not_accepted(archive, db, actor, seeded_scope, chat_request_factory):
    request = chat_request_factory(conversation_id=seeded_scope.conversation_id)
    request.input.asset_ids = [uuid4()]
    with pytest.raises(AppError):
        await archive.accept(actor, request)
    assert db.count("requests") == 0

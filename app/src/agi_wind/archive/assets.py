import hashlib
from io import BytesIO
import os
from pathlib import Path
import tempfile
import wave
from PIL import Image
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from . import models as m
from .repository import lock
from ..core.errors import AppError


class AssetStore:
    def __init__(self, db, root, max_bytes=10*1024*1024,max_image_bytes=10*1024*1024):
        self.db, self.root, self.max_bytes = db, Path(root).resolve(), max_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_image_bytes=max_image_bytes

    async def put(self, actor, stream, declared_mime):
        staged = None
        limit=min(self.max_bytes,self.max_image_bytes) if declared_mime.startswith("image/") else self.max_bytes
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as temp:
                staged = Path(temp.name)
                hasher, size = hashlib.sha256(), 0
                while block := stream.read(65536):
                    size += len(block)
                    if size > limit: raise AppError("ASSET_TOO_LARGE", "附件超过大小限制", 413)
                    temp.write(block)
                    hasher.update(block)
                temp.flush()
                os.fsync(temp.fileno())
            mime, ext, metadata = self.inspect(staged, declared_mime)
            sha = hasher.hexdigest()
            relative = Path(sha[:2]) / (sha + "." + ext)
            final = self.root / relative
            final.parent.mkdir(parents=True, exist_ok=True)
            with self.db.engine.begin() as conn:
                lock(conn, "asset:" + sha)
                row = conn.execute(select(m.assets).where(m.assets.c.sha256 == sha)).mappings().first()
                if not final.exists(): os.replace(staged, final)
                if row is None:
                    row = conn.execute(m.assets.insert().values(sha256=sha, relative_path=relative.as_posix(), mime=mime, bytes=size, kind="image" if mime.startswith("image/") else "audio", metadata=metadata).returning(m.assets)).mappings().one()
                conn.execute(insert(m.asset_owners).values(asset_id=row["id"], account_id=actor.account_id).on_conflict_do_nothing())
            return dict(row)
        finally:
            if staged: staged.unlink(missing_ok=True)

    @staticmethod
    def inspect(path, declared_mime):
        try:
            if declared_mime in ("image/png", "image/jpeg", "image/webp"):
                with Image.open(path) as image:
                    mime = Image.MIME.get(image.format)
                    if mime != declared_mime or image.width * image.height > 25_000_000:
                        raise ValueError("MIME/dimension mismatch")
                    metadata = {"width": image.width, "height": image.height}
                    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
                    image.verify()
                with Image.open(path) as image: image.load()
                return mime, ext, metadata
            if declared_mime in ("audio/wav", "audio/x-wav"):
                with wave.open(str(path), "rb") as audio:
                    frames, rate = audio.getnframes(), audio.getframerate()
                    expected = frames * audio.getnchannels() * audio.getsampwidth()
                    if not rate or frames <= 0 or frames / rate > 120 or len(audio.readframes(frames)) != expected:
                        raise ValueError("invalid audio duration/data")
                    return "audio/wav", "wav", {"sample_rate": rate, "channels": audio.getnchannels(), "duration_ms": round(frames * 1000 / rate)}
        except Exception:
            raise AppError("INVALID_MEDIA", "附件无法解码或类型不符", 422) from None
        raise AppError("UNSUPPORTED_MEDIA", "不支持的附件类型", 422)

    def get(self, actor, asset_id):
        with self.db.engine.connect() as conn:
            owner = conn.execute(select(m.asset_owners).where(m.asset_owners.c.asset_id == asset_id, m.asset_owners.c.account_id == actor.account_id)).first()
            if not owner: raise AppError("FORBIDDEN", "无权读取此附件", 403)
            row = conn.execute(select(m.assets).where(m.assets.c.id == asset_id)).mappings().one()
        path = (self.root / row["relative_path"]).resolve()
        if not path.is_relative_to(self.root): raise AppError("INVALID_ASSET_PATH", "附件路径无效", 422)
        return dict(row), path

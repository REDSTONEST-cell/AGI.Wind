"""QQ group rooms: normalization, persistent window binding and send receipts.

One QQ group is one room and one persistent conversation. Member identity is the
(platform, external_user_id) pair; nickname/card are display-only. Nothing here
creates an owner permission from group text or from OneBot `sender.role`.
"""
import logging
import time
from uuid import UUID, uuid4

from sqlalchemy import select, update

from ..archive import models as m
from ..archive.repository import lock
from ..identity.service import authorize

logger = logging.getLogger("nagi.qq")

_MAX_SEGMENTS = 64
_MAX_TEXT = 12000
_MAX_IMAGES = 4


def _sender_id(value):
    text = str(value if value is not None else "").strip()
    return text if text.isdigit() and 5 <= len(text) <= 20 else ""


def group_message(data, settings):
    """Return one normalized group event or None. Pure: no DB, no replies."""
    if not isinstance(data, dict) or not getattr(settings, "enabled", False) or not getattr(settings, "group_enabled", False):
        return None
    if (data.get("post_type") != "message" or data.get("message_type") != "group"
            or data.get("sub_type") not in (None, "normal")):
        return None
    bot_qq = _sender_id(getattr(settings, "bot_qq", ""))
    if not bot_qq or str(data.get("self_id")) != bot_qq:
        return None
    group_id = _sender_id(data.get("group_id"))
    sender_qq = _sender_id(data.get("user_id"))
    if not group_id or not sender_qq or data.get("message_id") is None:
        return None
    if data.get("time") is not None:
        try:
            if abs(time.time() - float(data["time"])) > 300:
                return None
        except (TypeError, ValueError):
            return None
    segments = data.get("message")
    if isinstance(segments, str):
        segments = [{"type": "text", "data": {"text": segments}}]
    if not isinstance(segments, list) or len(segments) > _MAX_SEGMENTS:
        return None
    text, images, image_files, records, mentioned, reply_to = [], [], [], [], [], None
    for part in segments:
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            continue
        kind, value = part.get("type"), part["data"]
        if kind == "text":
            text.append(str(value.get("text", "")))
        elif kind == "image" and len(images)+len(image_files) < _MAX_IMAGES:
            url = value.get("url")
            if isinstance(url, str) and len(url) <= 4000:
                images.append(url)
            elif isinstance(value.get('file'), str) and 0 < len(value['file']) <= 4000:
                image_files.append(value['file'])
        elif kind == 'record' and not records:
            file = value.get('file')
            if isinstance(file, str) and 0 < len(file) <= 4000:
                records.append(file)
        elif kind == "at":
            target = _sender_id(value.get("qq"))
            if target:
                mentioned.append(target)
        elif kind == "reply":
            target = str(value.get("id") or "").strip()
            if target:
                reply_to = target
    joined = "".join(text).strip()
    if len(joined) > _MAX_TEXT or (not joined and not images and not image_files and not records) or sender_qq == bot_qq:
        return None
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    anonymous = bool(data.get("anonymous"))
    return {
        "kind": "group", "group_id": group_id, "bot_qq": bot_qq,
        "platform_message_id": str(data.get("message_id")),
        "sender_qq": sender_qq,
        "sender_nickname": str(sender.get("nickname") or "")[:200],
        "sender_card": str(sender.get("card") or "")[:200],
        "sender_group_role": str(sender.get("role") or "")[:40],
        "anonymous": anonymous,
        "text": joined, "images": images, "image_files": image_files, "records": records,
        "mentioned_qq_ids": list(dict.fromkeys(mentioned)),
        "reply_to_platform_message_id": reply_to,
        "occurred_at": float(data["time"]) if data.get("time") is not None else None,
    }


def group_allowed(settings, group_id):
    if not getattr(settings, "group_enabled", False):
        return False
    return str(group_id) in set(getattr(settings, "allowed_groups", []) or [])


def ensure_principal(db, actor, *, external_user_id, nickname="", card="", is_owner=False, platform="qq"):
    """Stable member identity; card/nickname changes never create a new person."""
    external_user_id = str(external_user_id)
    with db.engine.begin() as conn:
        lock(conn, f"principal:{actor.account_id}:{platform}:{external_user_id}")
        row = conn.execute(select(m.external_principals).where(
            m.external_principals.c.account_id == actor.account_id,
            m.external_principals.c.platform == platform,
            m.external_principals.c.external_user_id == external_user_id)).mappings().first()
        if row:
            values = {"last_seen_at": m.now(), "is_owner": bool(is_owner)}
            if (nickname or "") != row["display_name"]:
                values["display_name"] = nickname or ""
            if (card or "") != row["card_name"]:
                values["card_name"] = card or ""
            result = conn.execute(update(m.external_principals).where(
                m.external_principals.c.id == row["id"]).values(**values).returning(m.external_principals)).mappings().one()
            return dict(result)
        return dict(conn.execute(m.external_principals.insert().values(
            account_id=actor.account_id, platform=platform, external_user_id=external_user_id,
            display_name=nickname or "", card_name=card or "", is_owner=bool(is_owner),
            last_seen_at=m.now()).returning(m.external_principals)).mappings().one())


def ensure_group_binding(db, actor, *, companion_id, bot_qq, group_id, title=""):
    """Create or reuse this group's persistent room window. Never archives other rooms."""
    group_id, bot_qq = str(group_id), str(bot_qq)
    with db.engine.begin() as conn:
        authorize(conn, actor, companion_id)
        lock(conn, companion_id)
        binding = conn.execute(select(m.channel_bindings).where(
            m.channel_bindings.c.account_id == actor.account_id,
            m.channel_bindings.c.companion_id == companion_id,
            m.channel_bindings.c.platform == "qq",
            m.channel_bindings.c.bot_external_id == bot_qq,
            m.channel_bindings.c.channel_kind == "group",
            m.channel_bindings.c.external_chat_id == group_id)).mappings().first()
        if binding:
            conversation = conn.execute(select(m.conversations).where(
                m.conversations.c.id == binding["conversation_id"])).mappings().first()
            if conversation:
                values = {"enabled": True, "updated_at": m.now()}
                if title and title != binding["title"]:
                    values["title"] = title
                if conversation["state"] != "active":
                    values["conversation_id"] = binding["conversation_id"]
                    conn.execute(update(m.conversations).where(m.conversations.c.id == conversation["id"]).values(state="active"))
                binding = conn.execute(update(m.channel_bindings).where(
                    m.channel_bindings.c.id == binding["id"]).values(**values).returning(m.channel_bindings)).mappings().one()
                if title and title != conversation["title"]:
                    conn.execute(update(m.conversations).where(m.conversations.c.id == conversation["id"]).values(title=title))
                return dict(binding)
        conversation = conn.execute(m.conversations.insert().values(
            account_id=actor.account_id, companion_id=companion_id, user_id=actor.user_id,
            title=(title or f"QQ群 {group_id}")[:200], state="active",
            channel_kind="group", external_chat_id=group_id).returning(m.conversations)).mappings().one()
        values = dict(account_id=actor.account_id, companion_id=companion_id, platform="qq",
                      bot_external_id=bot_qq, channel_kind="group", external_chat_id=group_id,
                      conversation_id=conversation["id"], title=(title or f"QQ群 {group_id}")[:200],
                      enabled=True, updated_at=m.now())
        if binding:
            return dict(conn.execute(update(m.channel_bindings).where(
                m.channel_bindings.c.id == binding["id"]).values(**values).returning(m.channel_bindings)).mappings().one())
        return dict(conn.execute(m.channel_bindings.insert().values(**values).returning(m.channel_bindings)).mappings().one())


def receipt_exists(db, binding_id, platform_message_id):
    with db.engine.connect() as conn:
        return bool(conn.execute(select(m.qq_message_receipts.c.id).where(
            m.qq_message_receipts.c.binding_id == binding_id,
            m.qq_message_receipts.c.platform_message_id == str(platform_message_id))).first())


def save_send_receipt(db, binding_id, platform_message_id, generation_id=None, segment_index=0):
    """Remember every delivered segment id so a later quote of segment 2/3 is recognized."""
    if platform_message_id is None:
        return None
    with db.engine.begin() as conn:
        from sqlalchemy.dialects.postgresql import insert
        return conn.execute(insert(m.qq_message_receipts).values(
            account_id=conn.scalar(select(m.channel_bindings.c.account_id).where(
                m.channel_bindings.c.id == binding_id)),
            binding_id=binding_id, platform_message_id=str(platform_message_id),
            generation_id=generation_id, segment_index=int(segment_index))
            .on_conflict_do_nothing().returning(m.qq_message_receipts.c.id)).scalar()

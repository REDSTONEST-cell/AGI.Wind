"""Server-enforced memory pool and scene visibility policy.

Pools are logical ownership, scenes are where a reply is being produced:

* owner_private: the owner's own memory pool only. Group chat transcripts are not
  mixed into private prompts; the owner's own group statements are still their facts.
* group: the owner facts explicitly published in this group plus member facts that
  belong to this group. Another group, another member and any private fact stay out.

The model and request text never construct these scopes.
"""
from sqlalchemy import and_, exists, or_, select

from . import models as mm
from ..archive import models as m
from ..core.types import SceneScope


def scene_of(scope):
    return getattr(scope, "scene", "owner_private")


def room_of(scope):
    return getattr(scope, "room_id", None)


def item_pool_clause(scope):
    """Pool/visibility predicate for memory_items (identity scope is separate)."""
    a = mm.memory_items.c
    if scene_of(scope) == "group":
        room = room_of(scope)
        return and_(or_(
            and_(a.pool_kind == "owner", a.visibility == "group_public", a.visibility_room_id == room),
            and_(a.pool_kind == "member", a.visibility == "group_public", a.visibility_room_id == room),
        ))
    return a.pool_kind == "owner"


def conversation_pool_clause(scope, conversation=m.conversations):
    """Which conversations may supply raw recent/chunk context for this scene."""
    if scene_of(scope) == "group":
        return conversation.c.id == scope.conversation_id
    return conversation.c.channel_kind != "group"


def owner_authored_clause(events=m.events):
    """Owner-pool extraction only trusts the owner's own messages.

    Legacy private rows may lack a principal. Missing group identity is never
    evidence that the speaker is the owner (also excludes unaddressed bot replies).
    """
    principal = m.external_principals
    private=exists(select(m.conversations.c.id).where(m.conversations.c.id==events.c.conversation_id,
        m.conversations.c.channel_kind!='group').correlate(events))
    return or_(and_(events.c.sender_principal_id.is_(None),private), exists(select(principal.c.id).where(
        principal.c.id == events.c.sender_principal_id, principal.c.is_owner.is_(True))))


def ref_visible_in_scene(scope, row):
    """Python-side recheck for one verified source reference row."""
    if scene_of(scope) == "group":
        return row.get("conversation_id") == scope.conversation_id
    if row.get("channel_kind") != "group":
        return True
    # The owner's own group statement may be quoted in a private window; a member's
    # statement may not, even when the owner is the account holder.
    return bool(row.get("sender_is_owner"))


def speaker_label(row, principal=None):
    """Display label: group card, then nickname, then QQ number. Never authorization."""
    if principal:
        return (principal.get("card_name") or principal.get("display_name")
                or principal.get("external_user_id") or "群成员")
    return "群成员"

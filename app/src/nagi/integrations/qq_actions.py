"""Pure OneBot v11 action helpers and multi-account QQ configuration models.

Everything in this module is deterministic: no database, no network, no FastAPI
and no settings import. ``qq.py`` owns the bridge and calls these helpers; unit
tests exercise them without any OneBot server.

Design boundaries mirror :mod:`qq_groups`/``qq_attention``:

* the account list is the authority for bot/owner/groups/token, while the
  existing single-account fields remain a compatibility fallback (one synthetic
  ``default`` account) so older settings files keep working;
* OneBot payloads are assembled from bounded server-side data only — message
  ids are validated and forward nodes are capped, never trusted verbatim.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable

from pydantic import Field, SecretStr, field_validator, model_validator

from ..core.types import DTO

QQ_NUMBER = r'^(?:[1-9][0-9]{4,19})?$'
GROUP_LENGTH = (5, 20)
MAX_FORWARD_NODES = 20
MAX_FORWARD_CHARS = 4000
MAX_FORWARD_IDS = 3
MAX_APPROVAL_DETAIL = 300
# Existing single-account deployments stored the token under this reference.
TOKEN_REF = 'NAGI_QQ_ONEBOT_TOKEN'
APPROVAL_TOKENS = {'#批准': 'approve', '#拒绝': 'reject'}
_MASKED_SECRET = '**********'


def _group_ids(values):
    result = []
    for value in values:
        text = str(value).strip()
        if not text.isdigit() or not GROUP_LENGTH[0] <= len(text) <= GROUP_LENGTH[1]:
            raise ValueError('group ids must be QQ group numbers')
        if text not in result:
            result.append(text)
    return result


class QQAccountSettings(DTO):
    """One QQ bot connection under an Nagi account.

    ``enabled`` is the account-level switch; the global ``QQSettings.enabled``
    remains the master switch. Tokens normally live in the SecretStore
    (:func:`token_ref_for`); an inline secret is accepted for tests/imports and
    is masked as soon as settings are persisted.
    """
    account_id: str = Field(default='default', min_length=1, max_length=64,
                            pattern=r'^[A-Za-z0-9_.-]+$')
    bot_qq: str = Field(default='', pattern=QQ_NUMBER)
    owner_qq: str = Field(default='', pattern=QQ_NUMBER)
    onebot_ws_url: str = Field(default='', max_length=500)
    onebot_token: SecretStr | None = None
    allowed_groups: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True

    @field_validator('onebot_ws_url')
    @classmethod
    def ws_url(cls, value):
        text = str(value or '').strip()
        if not text:
            return ''
        if len(text) > 500 or any(character in text for character in ('\r', '\n', '\x00')):
            raise ValueError('onebot_ws_url is invalid')
        if not text.startswith(('ws://', 'wss://')):
            raise ValueError('onebot_ws_url must use ws:// or wss://')
        return text

    @field_validator('allowed_groups')
    @classmethod
    def group_ids(cls, values):
        return _group_ids(values)

    @model_validator(mode='after')
    def binding(self):
        if self.bot_qq and self.owner_qq and self.bot_qq == self.owner_qq:
            raise ValueError('owner and bot must differ')
        if self.enabled and (not self.bot_qq or not self.owner_qq):
            raise ValueError('enabled QQ account requires bot and owner')
        return self


def token_ref_for(account_id: str) -> str:
    """SecretStore reference for one account; ``default`` keeps the legacy ref."""
    text = str(account_id or 'default').strip() or 'default'
    if text == 'default':
        return TOKEN_REF
    cleaned = ''.join(character if character.isalnum() else '_' for character in text).upper()
    return f'{TOKEN_REF}_{cleaned or "DEFAULT"}'


def resolve_accounts(policy) -> list[QQAccountSettings]:
    """Return the configured accounts, or a synthetic one from legacy fields."""
    raw = list(getattr(policy, 'accounts', None) or ())
    if raw:
        return [item if isinstance(item, QQAccountSettings) else QQAccountSettings.model_validate(item)
                for item in raw]
    return [QQAccountSettings(
        account_id='default',
        bot_qq=str(getattr(policy, 'bot_qq', '') or ''),
        owner_qq=str(getattr(policy, 'owner_qq', '') or ''),
        allowed_groups=list(getattr(policy, 'allowed_groups', None) or ()),
        enabled=bool(getattr(policy, 'enabled', False)),
    )]


def active_accounts(policy) -> list[QQAccountSettings]:
    """Accounts that may connect: enabled master switch and a usable identity."""
    if not bool(getattr(policy, 'enabled', False)):
        return []
    return [account for account in resolve_accounts(policy)
            if account.enabled and account.bot_qq and account.owner_qq]


def account_for_bot(policy, bot_qq) -> QQAccountSettings | None:
    text = str(bot_qq or '').strip()
    if not text:
        return None
    return next((account for account in resolve_accounts(policy) if account.bot_qq == text), None)


def account_for_id(policy, account_id) -> QQAccountSettings | None:
    text = str(account_id or '').strip()
    if not text:
        return None
    return next((account for account in resolve_accounts(policy) if account.account_id == text), None)


def inline_token(account: QQAccountSettings | None) -> str | None:
    """Unwrap a configured inline secret, ignoring a persisted mask."""
    token = getattr(account, 'onebot_token', None) if account is not None else None
    if token is None:
        return None
    raw = token.get_secret_value() if hasattr(token, 'get_secret_value') else str(token)
    raw = raw.strip() if isinstance(raw, str) else ''
    if not raw or raw == _MASKED_SECRET:
        return None
    return raw


# ---------------------------------------------------------------------------
# Message payload segments
# ---------------------------------------------------------------------------

def quote_segment(message_id) -> dict | None:
    """OneBot reply segment referencing a bounded numeric message id."""
    text = str(message_id if message_id is not None else '').strip()
    if not text or len(text) > 64 or not text.isdigit():
        return None
    return {'type': 'reply', 'data': {'id': text}}


def with_quote(parts: Iterable[dict], message_id, enabled: bool = True) -> list[dict]:
    """Prepend a quote segment to the first outgoing bubble when enabled."""
    result = [part for part in (parts or ()) if isinstance(part, dict)]
    if not enabled or not result:
        return result
    segment = quote_segment(message_id)
    if segment is None or any(part.get('type') == 'reply' for part in result):
        return result
    return [segment, *result]


def forward_ids(segments, limit: int = MAX_FORWARD_IDS) -> list[str]:
    """Bounded forward segment ids from an inbound message array."""
    result: list[str] = []
    if not isinstance(segments, list):
        return result
    for part in segments[:64]:
        if len(result) >= max(0, int(limit)):
            break
        if not isinstance(part, dict) or part.get('type') != 'forward':
            continue
        data = part.get('data') if isinstance(part.get('data'), dict) else {}
        value = data.get('id', data.get('resid'))
        text = str(value).strip() if value is not None else ''
        if text and len(text) <= 200 and text not in result:
            result.append(text)
    return result


def _node_text(content) -> str:
    """Flatten one forward node's content into bounded display text."""
    if isinstance(content, str):
        return content.strip()[:2000]
    if not isinstance(content, list):
        return ''
    parts: list[str] = []
    for part in content[:64]:
        if not isinstance(part, dict):
            continue
        kind = part.get('type')
        data = part.get('data') if isinstance(part.get('data'), dict) else {}
        if kind == 'text':
            parts.append(str(data.get('text', '')))
        elif kind == 'image':
            parts.append('[图片]')
        elif kind == 'face':
            parts.append('[表情]')
        elif kind == 'record':
            parts.append('[语音]')
        elif kind == 'video':
            parts.append('[视频]')
        elif kind == 'forward':
            parts.append('[合并转发]')
        elif kind == 'at':
            parts.append('[提及]')
    return ''.join(parts).strip()


def flatten_forward(payload, *, max_nodes: int = MAX_FORWARD_NODES,
                    max_chars: int = MAX_FORWARD_CHARS) -> str:
    """Flatten ``get_forward_msg`` payloads, bounded by nodes and characters.

    The result is untrusted conversation text: callers prepend a marker and pass
    it through the normal message path, never treat it as instructions.
    """
    if max_nodes <= 0 or max_chars <= 0:
        return ''
    nodes = payload.get('messages') if isinstance(payload, dict) else payload
    if not isinstance(nodes, list):
        return ''
    lines: list[str] = []
    used = 0
    for node in nodes[:int(max_nodes)]:
        if not isinstance(node, dict):
            continue
        sender = node.get('sender') if isinstance(node.get('sender'), dict) else {}
        name = str(sender.get('card') or sender.get('nickname') or sender.get('user_id') or '').strip()[:40]
        content = node.get('content') if node.get('content') is not None else node.get('message')
        text = _node_text(content)
        if not text:
            continue
        line = f'{name}: {text}' if name else text
        remaining = max_chars - used
        if remaining <= 0:
            break
        line = line[:remaining]
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ''
    return '\n'.join(lines)[:int(max_chars)]


def recall_payload(message_id) -> dict | None:
    """OneBot ``delete_msg`` payload for a numeric message id."""
    text = str(message_id if message_id is not None else '').strip()
    if not text.isdigit() or len(text) > 64:
        return None
    return {'message_id': int(text)}


# ---------------------------------------------------------------------------
# Poke notices
# ---------------------------------------------------------------------------

def poke_target(data, bot_qq) -> dict | None:
    """Return the OneBot ``send_poke`` payload when ``data`` pokes this bot."""
    if not isinstance(data, dict):
        return None
    if (data.get('post_type') != 'notice' or data.get('notice_type') != 'notify'
            or data.get('sub_type') != 'poke'):
        return None
    bot = str(bot_qq or '').strip()
    target = str(data.get('target_id') if data.get('target_id') is not None else data.get('target') or '').strip()
    user = str(data.get('user_id') or '').strip()
    if not bot or target != bot or user == bot:
        return None
    if not user.isdigit() or not GROUP_LENGTH[0] <= len(user) <= GROUP_LENGTH[1]:
        return None
    payload: dict[str, Any] = {'user_id': user}
    group = str(data.get('group_id') or '').strip()
    if group:
        if not group.isdigit() or not GROUP_LENGTH[0] <= len(group) <= GROUP_LENGTH[1]:
            return None
        payload['group_id'] = group
    return payload


class PokeRateLimiter:
    """Per-target poke budget: one cooldown interval and a rolling hourly cap."""

    def __init__(self, *, min_interval_seconds: float = 10.0, max_per_hour: int = 20,
                 clock: Callable[[], float] | None = None, window_seconds: float = 3600.0):
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.max_per_hour = max(0, int(max_per_hour))
        self.window_seconds = max(1.0, float(window_seconds))
        self._clock = clock or time.monotonic
        self._last: dict[str, float] = {}
        self._windows: dict[str, list[float]] = {}

    def allow(self, key) -> bool:
        text = str(key or '').strip()
        if not text:
            return False
        now = float(self._clock())
        last = self._last.get(text)
        if last is not None and 0 <= now - last < self.min_interval_seconds:
            return False
        window = [value for value in self._windows.get(text, ()) if 0 <= now - value < self.window_seconds]
        if len(window) >= self.max_per_hour:
            self._windows[text] = window[-self.max_per_hour:] if self.max_per_hour else []
            return False
        window.append(now)
        self._windows[text] = window[-max(1, self.max_per_hour):]
        self._last[text] = now
        if len(self._last) > 500:
            keep = sorted(self._last.items(), key=lambda item: item[1])[-400:]
            self._last = dict(keep)
        if len(self._windows) > 500:
            keep = sorted(self._windows.items(), key=lambda item: item[1][-1])[-400:]
            self._windows = dict(keep)
        return True


# ---------------------------------------------------------------------------
# History catch-up
# ---------------------------------------------------------------------------

def catchup_key(bot_qq, chat_kind, chat_id, message_id) -> str:
    """Stable dedupe key for one history row; empty when any part is invalid."""
    values = [str(value if value is not None else '').strip()
              for value in (bot_qq, chat_kind, chat_id, message_id)]
    if any(not value or len(value) > 100 for value in values):
        return ''
    if values[1] not in ('private', 'group'):
        return ''
    return '|'.join(('catchup', *values))


class CatchupDeduper:
    """Bounded in-memory set of already processed catch-up keys."""

    def __init__(self, maxlen: int = 2048):
        self.maxlen = max(1, int(maxlen))
        self._keys: set[str] = set()
        self._order: Iterable[str] = []

    def __len__(self) -> int:
        return len(self._keys)

    def is_new(self, key) -> bool:
        text = str(key or '').strip()
        if not text:
            return False
        if text in self._keys:
            return False
        self._keys.add(text)
        order = list(self._order) + [text]
        if len(order) > self.maxlen:
            order = order[-self.maxlen:]
            self._keys = set(order)
        self._order = order
        return True


def _history_message(event: dict, limit: int) -> list[dict] | None:
    message = event.get('message')
    if message is None:
        message = event.get('content')
    if isinstance(message, str):
        return [{'type': 'text', 'data': {'text': message}}]
    if isinstance(message, list):
        return message[:64]
    return None


def catchup_private_events(rows, *, bot_qq, owner_qq, limit: int = 20) -> list[dict]:
    """Convert OneBot private history rows into inbound events.

    ``time`` is deliberately omitted: the live path rejects events older than
    five minutes, while catch-up intentionally processes messages that arrived
    during a disconnect. The normal dedupe path still runs afterwards.
    """
    events: list[dict] = []
    if not isinstance(rows, list):
        return events
    limit = max(0, int(limit))
    for row in rows[-limit:] if limit else []:
        if not isinstance(row, dict):
            continue
        message_id = row.get('message_id', row.get('id'))
        if message_id is None:
            continue
        sender = row.get('sender') if isinstance(row.get('sender'), dict) else {}
        user_id = row.get('user_id') or sender.get('user_id')
        if str(user_id or '') != str(owner_qq):
            continue
        message = _history_message(row, limit)
        if not message:
            continue
        events.append({'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                       'self_id': bot_qq, 'user_id': owner_qq, 'message_id': message_id,
                       'message': message, 'sender': sender})
    return events


# ---------------------------------------------------------------------------
# Approval relay
# ---------------------------------------------------------------------------

def parse_approval_token(text) -> str | None:
    """Only the two explicit QQ commands are approvals; never free-form text."""
    value = str(text if text is not None else '').strip()
    return APPROVAL_TOKENS.get(value)


def approval_relay_text(row) -> str:
    """Chinese notice sent to the owner when a tool call needs approval."""
    operation = str((row or {}).get('operation') or '').strip()[:80] if isinstance(row, dict) else ''
    details = row.get('details') if isinstance(row, dict) else None
    try:
        rendered = json.dumps(details, ensure_ascii=False) if details is not None else ''
    except (TypeError, ValueError):
        rendered = ''
    if len(rendered) > MAX_APPROVAL_DETAIL:
        rendered = rendered[:MAX_APPROVAL_DETAIL] + '…'
    lines = [f'Nagi 需要你批准一个操作：{operation or "未知操作"}']
    if rendered:
        lines.append(f'详情：{rendered}')
    lines.append('回复 #批准 允许执行，回复 #拒绝 取消本次操作。')
    return '\n'.join(lines)


def approval_result_text(row, allowed: bool) -> str:
    operation = str((row or {}).get('operation') or '').strip()[:80] if isinstance(row, dict) else ''
    suffix = f'：{operation}' if operation else ''
    return ('已批准' if allowed else '已拒绝') + suffix


__all__ = [
    'APPROVAL_TOKENS', 'CatchupDeduper', 'MAX_FORWARD_CHARS', 'MAX_FORWARD_NODES',
    'PokeRateLimiter', 'QQAccountSettings', 'QQ_NUMBER', 'TOKEN_REF',
    'account_for_bot', 'account_for_id', 'active_accounts', 'approval_relay_text',
    'approval_result_text', 'catchup_key', 'catchup_private_events', 'flatten_forward',
    'forward_ids', 'inline_token', 'parse_approval_token', 'poke_target', 'quote_segment',
    'recall_payload', 'resolve_accounts', 'token_ref_for', 'with_quote',
]

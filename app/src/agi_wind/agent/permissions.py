"""Ephemeral, account-bound approvals. A restart or cancelled turn revokes them."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from ..core.errors import AppError


class ApprovalQueue:
    def __init__(self, timeout=120):
        self.pending = {}
        self.timeout = timeout

    @staticmethod
    def owner(actor):
        return str(actor.account_id), str(actor.user_id)

    async def request(self, actor, generation_id, operation, details):
        if len(self.pending) >= 16:
            raise AppError('APPROVAL_BUSY', '待批准操作过多', 409)
        key = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        row = {'id': key, 'generation_id': str(generation_id), 'operation': operation,
               'details': details, 'created_at': datetime.now(timezone.utc).isoformat()}
        self.pending[key] = (self.owner(actor), row, future)
        try:
            try:
                allowed = await asyncio.wait_for(future, self.timeout)
            except TimeoutError:
                raise AppError('TOOL_APPROVAL_TIMEOUT', '等待用户批准超时，文件操作已取消', 408) from None
            if not allowed:
                raise AppError('TOOL_APPROVAL_DENIED', '用户拒绝了本次文件操作，请停止重复请求', 403)
        finally:
            self.pending.pop(key, None)

    def list(self, actor):
        return [row for owner, row, future in self.pending.values() if owner == self.owner(actor) and not future.done()]

    def decide(self, actor, key, allow):
        item = self.pending.get(key)
        if not item or item[0] != self.owner(actor) or item[2].done():
            raise AppError('APPROVAL_NOT_FOUND', '请求已过期或无权访问', 404)
        item[2].set_result(allow)

    def revoke(self, actor=None):
        for owner, _, future in self.pending.values():
            if actor is not None and owner != self.owner(actor): continue
            if not future.done(): future.set_result(False)


def is_administrator():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False

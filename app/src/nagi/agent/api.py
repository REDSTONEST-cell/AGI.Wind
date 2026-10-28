import ipaddress
from pathlib import Path
import subprocess
import sys
from typing import Literal
from uuid import uuid4

from sqlalchemy import select, or_

from fastapi import Depends, Request
from pydantic import Field, SecretStr

from ..core.types import DTO
from ..core.errors import AppError
from ..core.settings import persist_settings, SETTINGS_WRITE_LOCK
from .settings import AgentSettings, search_secret_ref
from .search import WebSearch, SearchCredentials
from .permissions import ApprovalQueue, is_administrator


class ApprovalDecision(DTO):
    allow: bool


class AdminRequest(DTO):
    confirmed: Literal[True]


class AgentPatch(DTO):
    enabled: bool | None = None
    memory_search_enabled: bool | None = None
    sticker_search_enabled: bool | None = None
    sticker_send_enabled: bool | None = None
    web_search_enabled: bool | None = None
    file_access_mode: Literal['disabled', 'ask', 'full'] | None = None
    command_access_mode: Literal['disabled', 'ask', 'full'] | None = None
    command_timeout_seconds: float | None = Field(default=None, ge=1, le=300)
    command_max_output_chars: int | None = Field(default=None, ge=500, le=16000)
    max_rounds: int | None = Field(default=None, ge=1, le=8)
    max_tool_calls: int | None = Field(default=None, ge=1, le=16)
    tool_timeout_seconds: float | None = Field(default=None, gt=0, le=60)
    total_timeout_seconds: float | None = Field(default=None, ge=1, le=300)
    max_result_chars: int | None = Field(default=None, ge=500, le=16000)
    search_backend: str | None = None
    search_url: str | None = Field(default=None, max_length=2000)
    search_max_results: int | None = Field(default=None, ge=1, le=8)
    api_key: SecretStr | None = Field(default=None, max_length=4000)
    clear_key: bool = False


def install_agent_routes(app, settings, store, service, authenticate):
    service.agent_settings = lambda: settings.agent
    search_store = SearchCredentials(store, lambda: settings)
    service.agent_secret_store = search_store
    service.agent_approvals = approvals = ApprovalQueue()
    service.agent_data_dir = settings.data_dir

    @app.post('/api/v1/agent/stop')
    async def stop_actions(current=Depends(authenticate)):
        idle = getattr(service, 'idle_cognition', None)
        idle_stopped = idle.cancel_actor(current) if idle is not None else 0
        proactive=getattr(app.state,'proactive',None)
        proactive_stopped=proactive.cancel_actor(current) if proactive is not None else 0
        stop_calls=getattr(app.state,'stop_calls',None)
        call_stopped=await stop_calls(current) if stop_calls else 0
        qq_bridge=getattr(app.state,'qq_bridge',None)
        if qq_bridge: await qq_bridge.stop()
        from ..archive import models as m
        from ..delivery import models as d
        with service.db.engine.connect() as conn:
            ids = list(conn.scalars(select(m.generations.c.id).select_from(
                m.generations.join(m.requests, m.generations.c.request_id == m.requests.c.id)
                .join(m.conversations, m.requests.c.conversation_id == m.conversations.c.id)
                .outerjoin(d.speech_segments, d.speech_segments.c.generation_id == m.generations.c.id)
            ).where(m.conversations.c.account_id == current.account_id,
                    m.conversations.c.user_id == current.user_id,
                    or_(m.generations.c.state.in_(['pending','running','draining']), d.speech_segments.c.state.in_(['queued','synthesizing']))).distinct()))
        for generation_id in ids:
            await service.interrupt(current, generation_id, uuid4())
        if idle is not None:
            idle_stopped += idle.cancel_actor(current)
        approvals.revoke(current)
        return {'stopped': len(ids) + idle_stopped + proactive_stopped + call_stopped,
                'idle_stopped': idle_stopped,'proactive_stopped':proactive_stopped,'call_stopped':call_stopped}

    service.stop_all = stop_actions

    def view():
        policy = settings.agent
        provider = service.provider
        configured = WebSearch(policy, search_store).configured
        dedicated_key = store.configured(search_secret_ref(policy.search_backend))
        return {**policy.model_dump(mode='json'),
                'administrator': is_administrator(),
                'search_key_configured': bool(search_store.get(search_secret_ref(policy.search_backend))),
                'search_key_inherited': bool(policy.search_backend == 'deepseek' and configured and not dedicated_key),
                'search_ready': bool(policy.web_search_enabled and configured),
                'model_tools_available': bool(callable(getattr(provider, 'tool_turn', None)) or getattr(service, 'provider_resolver', None)),
                'tools': [
                    {'name': 'search_memory', 'label': '记忆查询', 'available': policy.memory_search_enabled},
                    {'name': 'search_stickers', 'label': '表情包查询', 'available': policy.sticker_search_enabled},
                    {'name': 'send_sticker', 'label': '发送表情包', 'available': policy.sticker_send_enabled and policy.sticker_search_enabled},
                    {'name': 'web_search', 'label': '联网检索', 'available': policy.web_search_enabled and configured},
                    {'name': 'web_fetch', 'label': '网页抓取', 'available': policy.web_search_enabled and policy.search_backend == 'deepseek' and configured},
                    {'name': 'computer_files', 'label': '电脑文件（' + {'disabled':'关闭','ask':'逐次批准','full':'完全访问'}[policy.file_access_mode] + '）', 'available': policy.file_access_mode != 'disabled'},
                    {'name': 'run_command', 'label': '执行命令行（' + {'disabled':'关闭','ask':'逐次批准','full':'完全允许'}[policy.command_access_mode] + '）', 'available': policy.command_access_mode != 'disabled'},
                ]}

    @app.get('/api/v1/agent/approvals')
    async def pending_approvals(current=Depends(authenticate)):
        return {'items': approvals.list(current)}

    @app.post('/api/v1/agent/approvals/{approval_id}')
    async def decide_approval(approval_id: str, body: ApprovalDecision, current=Depends(authenticate)):
        approvals.decide(current, approval_id, body.allow)
        return {'ok': True}

    @app.post('/api/v1/agent/admin/request')
    async def request_admin(body: AdminRequest, request: Request, current=Depends(authenticate)):
        try: local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError: local = False
        if not local or settings.host not in ('127.0.0.1', 'localhost', '::1'):
            raise AppError('LOCAL_ONLY', '管理员重启只能在本机、回环绑定时请求', 403)
        if is_administrator(): return {'already_administrator': True}
        if sys.platform != 'win32': raise AppError('WINDOWS_REQUIRED', '此按钮仅支持Windows', 422)
        helper = Path(__file__).resolve().parents[4] / 'scripts' / 'request-agent-admin.ps1'
        port = request.url.port or 18440
        try:
            subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper), '-Port', str(port)], creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            raise AppError('ADMIN_REQUEST_FAILED', '未能发起Windows管理员确认', 503) from None
        return {'requested': True, 'message': '请在Windows UAC窗口确认；接受后将重启本机服务'}

    @app.get('/api/v1/agent/settings')
    def read_settings(current=Depends(authenticate)):
        return view()

    @app.patch('/api/v1/agent/settings')
    def update_settings(body: AgentPatch, current=Depends(authenticate)):
        with SETTINGS_WRITE_LOCK:
            return apply_update(body)

    def apply_update(body):
        values = body.model_dump(exclude_unset=True, exclude_none=True, exclude={'api_key', 'clear_key'})
        try:
            updated = AgentSettings.model_validate({**settings.agent.model_dump(), **values})
        except ValueError:
            raise AppError('INVALID_AGENT_SETTINGS', 'Agent 设置无效，请检查检索服务地址和参数范围', 422) from None
        if body.api_key is not None and body.clear_key:
            raise AppError('INVALID_AGENT_SETTINGS', '不能同时保存和清除密钥', 422)
        if body.api_key is not None:
            key = body.api_key.get_secret_value().strip()
            if not key: raise AppError('INVALID_AGENT_SETTINGS', '密钥不能为空，清除请勾选清除密钥', 422)
            store.set(search_secret_ref(updated.search_backend), key)
        elif body.clear_key:
            store.delete(search_secret_ref(updated.search_backend))
        candidate = settings.model_copy(update={'agent': updated})
        persist_settings(candidate)
        settings.agent = updated
        if 'file_access_mode' in values or 'command_access_mode' in values or values.get('enabled') is False: approvals.revoke()
        return view()

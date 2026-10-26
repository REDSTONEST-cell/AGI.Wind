from sqlalchemy import select

from ..archive import models as m
from ..archive.repository import lock
from ..archive.stickers import attachment
from ..core.errors import AppError
from ..core.types import ModelRef
from ..identity.service import authorize
from .runner import AgentRunner
from .search import WebSearch
from .tools import StationTools


async def run_agent(service, generation_id, scope, generation, provider, model, messages):
    settings = service.agent_settings
    policy = settings().model_copy(deep=True)

    async def check():
        if not settings().enabled: raise AppError('AGENT_DISABLED', 'Agent 能力已关闭，已停止本轮工具执行', 409)
        with service.db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            authorize(conn, scope, scope.companion_id)
            service.assert_current(conn, generation_id, generation['memory_revision'], generation['epoch'])

    async def reasoning(text):
        await service.record_reasoning(generation_id, scope, generation, 'agent', text)

    async def trace(event):
        payload = dict(event)
        if event['tool'] in ('read_file', 'list_files') and isinstance(payload.get('result'), dict):
            result = payload['result']
            payload['result'] = {key: result[key] for key in ('path', 'sha256', 'bytes', 'truncated') if key in result}
        if event['tool'] == 'run_command' and isinstance(payload.get('result'), dict):
            result = payload['result']
            # stdout/stderr go to the temporary model tool result only; the durable
            # trace keeps the command envelope instead of a second full copy.
            payload['result'] = {key: result[key] for key in ('command', 'cwd', 'exit_code', 'truncated') if key in result}
        if event['tool'] == 'search_memory' and 'result' in payload:
            # Source text goes only to the temporary model tool result. A second
            # durable copy here would escape the source deletion/tombstone graph.
            result = payload['result']
            items = result.get('items', []) if isinstance(result, dict) else []
            payload['result'] = {
                'count': len(items),
                'truncated': bool(isinstance(result, dict) and result.get('truncated')),
                'memory_ids': [item['memory_id'] for item in items if item.get('memory_id')],
                'source_ids': sorted({ref['event_id'] for item in items for ref in item.get('source_refs', [])}),
            }
        def mutate(conn, content, row):
            steps = content.setdefault('trace', [])
            # Duplicate upstream IDs still get a distinct completed trace entry.
            if event['status'] == 'running':
                steps.append({'id': payload['step_id'], 'type': 'tool', **payload})
            else:
                step = next((item for item in reversed(steps) if item.get('id') == payload['step_id'] and item.get('status') == 'running'), None)
                if step: step.update(payload)
        await service.update_output(generation_id, scope, generation, mutate, 'tool.started' if event['status']=='running' else 'tool.completed', payload)

    async def send(sticker_id):
        payload = {'trigger': 'agent'}
        def mutate(conn, content, row):
            sticker = conn.execute(select(m.stickers).where(m.stickers.c.id==sticker_id, m.stickers.c.account_id==scope.account_id, m.stickers.c.companion_id==scope.companion_id, m.stickers.c.state=='active')).mappings().first()
            if not sticker: raise AppError('STICKER_UNAVAILABLE', '表情包已不可用', 404)
            owned = conn.execute(select(m.asset_owners).where(m.asset_owners.c.asset_id==sticker['asset_id'], m.asset_owners.c.account_id==scope.account_id)).first()
            if not owned: raise AppError('ASSET_UNAVAILABLE', '无权访问图片', 403)
            asset = conn.execute(select(m.assets).where(m.assets.c.id==sticker['asset_id'])).mappings().one()
            value = attachment(asset, 'sticker', sticker['name'], trigger='agent')
            content.setdefault('attachments', []).append(value)
            conn.execute(m.asset_refs.insert().values(event_id=row['id'], asset_id=asset['id'], purpose='output'))
            conn.execute(m.stickers.update().where(m.stickers.c.id==sticker_id).values(use_count=m.stickers.c.use_count+1,last_used_at=m.now()))
            payload['attachment'] = value
        await service.update_output(generation_id, scope, generation, mutate, 'sticker.sent', payload)
        return {'sent': True, 'sticker_id': str(sticker_id)}

    search = WebSearch(policy, getattr(service, 'agent_secret_store', None))
    tools = StationTools(service.db, scope, settings, search, send)
    specs = tools.specs()
    if getattr(service, 'agent_approvals', None) is not None:
        from .files import FileTools
        files = FileTools(service.agent_data_dir, settings, service.agent_approvals, scope, generation_id, check)
        specs += files.specs()
        from .commands import CommandTools
        commands = CommandTools(service.agent_data_dir, settings, service.agent_approvals, scope, generation_id, check)
        specs += commands.specs()
    result = await AgentRunner(policy, specs, on_event=trace, on_reasoning=reasoning, check_current=check).run(provider, model, messages)
    return result.text

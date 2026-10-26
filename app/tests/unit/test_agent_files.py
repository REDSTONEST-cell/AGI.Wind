import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agi_wind.agent.files import FileTools, FilePath, FileWrite, FileDownload
from agi_wind.agent.permissions import ApprovalQueue
from agi_wind.agent.settings import AgentSettings
from agi_wind.core.errors import AppError
from agi_wind.core.types import Actor


async def current(): pass


def setup(tmp_path, mode='ask'):
    settings = AgentSettings(enabled=True, file_access_mode=mode)
    queue = ApprovalQueue()
    actor = Actor(account_id=uuid4(), user_id=uuid4())
    files = FileTools(tmp_path, lambda: settings, queue, actor, uuid4(), current)
    return settings, queue, actor, files


async def test_file_requires_account_bound_approval_before_execution(tmp_path):
    settings, queue, actor, files = setup(tmp_path)
    target = tmp_path / 'approved.txt'
    args = FileWrite(path=str(target), content='批准后写入')
    with pytest.raises(AppError): await files.execute('write_file', args)
    task = asyncio.create_task(files.authorization('write_file')(args))
    await asyncio.sleep(0)
    assert not target.exists()
    request = queue.list(actor)[0]
    assert request['details']['content_preview'] == args.content
    stranger = Actor(account_id=uuid4(), user_id=uuid4())
    assert queue.list(stranger) == []
    with pytest.raises(AppError): queue.decide(stranger, request['id'], True)
    queue.decide(actor, request['id'], True)
    await task
    await files.execute('write_file', args)
    assert target.read_text(encoding='utf-8') == args.content
    with pytest.raises(AppError): await files.execute('write_file', args)


async def test_denial_cancel_and_disable_revoke_execution(tmp_path):
    settings, queue, actor, files = setup(tmp_path)
    args = FileWrite(path=str(tmp_path/'blocked.txt'), content='拒绝')
    task = asyncio.create_task(files.authorization('write_file')(args))
    await asyncio.sleep(0)
    queue.decide(actor, queue.list(actor)[0]['id'], False)
    with pytest.raises(AppError): await task
    assert not Path(args.path).exists() and not queue.list(actor)
    task = asyncio.create_task(files.authorization('write_file')(args))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not queue.list(actor)
    settings.file_access_mode = 'disabled'
    assert not files.specs()
    with pytest.raises(AppError): await files.execute('write_file', args)


async def test_full_access_edit_requires_hash_and_keeps_original_backup(tmp_path):
    _, _, _, files = setup(tmp_path, 'full')
    target = tmp_path/'outside-workspace.txt'
    target.write_text('原文', encoding='utf-8')
    original = await files.execute('read_file', FilePath(path=str(target)))
    with pytest.raises(AppError): await files.execute('write_file', FileWrite(path=str(target), content='不可盲目覆盖'))
    result = await files.execute('write_file', FileWrite(path=str(target), content='修改后', expected_sha256=original['sha256']))
    assert target.read_text(encoding='utf-8') == '修改后'
    assert Path(result['backup_path']).read_text(encoding='utf-8') == '原文'
    with pytest.raises(AppError): await files.execute('write_file', FileWrite(path=str(target), content='过期修改', expected_sha256=original['sha256']))
    assert target.read_text(encoding='utf-8') == '修改后'
    listing = await files.execute('list_files', FilePath(path=str(tmp_path)))
    assert any(row['name'] == target.name for row in listing['entries'])


async def test_download_saves_without_overwrite_or_execution(tmp_path, monkeypatch):
    import agi_wind.agent.files as module
    _, _, _, files = setup(tmp_path, 'full')
    monkeypatch.setattr(module.socket, 'getaddrinfo', lambda *a, **kw: [(2,1,6,'',('93.184.216.34',443))])
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: actual_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'public download')), **kw))
    target = tmp_path/'download.txt'
    args = FileDownload(path=str(target), url='https://example.org/file')
    await files.execute('download_file', args)
    assert target.read_bytes() == b'public download'
    with pytest.raises(AppError): await files.execute('download_file', args)
    monkeypatch.setattr(module.socket, 'getaddrinfo', lambda *a, **kw: [(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(AppError): await files.execute('download_file', FileDownload(path=str(tmp_path/'local.txt'), url='https://localhost/file'))


async def test_stale_turn_after_approval_does_not_write(tmp_path):
    _, queue, actor, files = setup(tmp_path)
    args = FileWrite(path=str(tmp_path/'stale.txt'), content='过期')
    task = asyncio.create_task(files.authorization('write_file')(args))
    await asyncio.sleep(0)
    async def stale(): raise AppError('STALE_CONTEXT', 'context changed', 409)
    files.check_current = stale
    queue.decide(actor, queue.list(actor)[0]['id'], True)
    with pytest.raises(AppError): await task
    assert not Path(args.path).exists()


async def test_approval_timeout_is_a_typed_error_and_cleans_up(tmp_path):
    _, queue, actor, files = setup(tmp_path)
    queue.timeout = 0.01
    args = FileWrite(path=str(tmp_path/'timeout.txt'), content='超时')
    with pytest.raises(AppError) as error:
        await files.authorization('write_file')(args)
    assert error.value.code == 'TOOL_APPROVAL_TIMEOUT'
    assert queue.list(actor) == []
    assert not Path(args.path).exists()


async def test_relative_paths_create_the_agent_workspace(tmp_path):
    _, _, _, files = setup(tmp_path, 'full')
    listing = await files.execute('list_files', FilePath(path='.'))
    assert listing['entries'] == []
    assert (tmp_path / 'agent-workspace').is_dir()
    written = await files.execute('write_file', FileWrite(path='notes/today.txt', content='工作区文本'))
    assert Path(written['path']).read_text(encoding='utf-8') == '工作区文本'
    nested = await files.execute('list_files', FilePath(path='notes'))
    assert any(row['name'] == 'today.txt' for row in nested['entries'])

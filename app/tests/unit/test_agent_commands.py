import asyncio
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from agi_wind.agent.commands import CommandArgs, CommandTools
from agi_wind.agent.permissions import ApprovalQueue
from agi_wind.agent.settings import AgentSettings
from agi_wind.core.errors import AppError
from agi_wind.core.types import Actor


async def current(): pass


def setup(tmp_path, mode='ask', **overrides):
    settings = AgentSettings(enabled=True, command_access_mode=mode, **overrides)
    queue = ApprovalQueue()
    actor = Actor(account_id=uuid4(), user_id=uuid4())
    tools = CommandTools(tmp_path, lambda: settings, queue, actor, uuid4(), current)
    return settings, queue, actor, tools


def echo(text):
    return 'echo ' + text


async def test_ask_mode_requires_approval_and_executes_after_allow(tmp_path):
    _, queue, actor, tools = setup(tmp_path)
    args = CommandArgs(command=echo('agent-command-ok'))
    with pytest.raises(AppError):
        await tools.execute(args)
    task = asyncio.create_task(tools.authorization()(args))
    await asyncio.sleep(0)
    request = queue.list(actor)[0]
    assert request['operation'] == 'run_command'
    assert 'agent-command-ok' in request['details']['command']
    queue.decide(actor, request['id'], True)
    await task
    result = await tools.execute(args)
    assert result['exit_code'] == 0
    assert 'agent-command-ok' in result['stdout']
    assert result['cwd'].endswith('agent-workspace')
    assert Path(result['cwd']).is_dir()
    # The permit is single-use: the same command cannot execute again unapproved.
    with pytest.raises(AppError):
        await tools.execute(args)


async def test_denied_command_never_runs(tmp_path):
    _, queue, actor, tools = setup(tmp_path)
    args = CommandArgs(command=echo('denied-command'))
    task = asyncio.create_task(tools.authorization()(args))
    await asyncio.sleep(0)
    queue.decide(actor, queue.list(actor)[0]['id'], False)
    with pytest.raises(AppError) as error:
        await task
    assert error.value.code == 'TOOL_APPROVAL_DENIED'
    with pytest.raises(AppError):
        await tools.execute(args)


async def test_full_mode_runs_without_approval(tmp_path):
    _, queue, actor, tools = setup(tmp_path, 'full')
    result = await tools.execute(CommandArgs(command=echo('full-access')))
    assert result['exit_code'] == 0
    assert 'full-access' in result['stdout']
    assert queue.list(actor) == []


async def test_timeout_kills_command(tmp_path):
    _, _, _, tools = setup(tmp_path, 'full', command_timeout_seconds=1)
    command = 'ping -n 6 127.0.0.1 > nul' if sys.platform == 'win32' else 'sleep 5'
    with pytest.raises(AppError) as error:
        await tools.execute(CommandArgs(command=command))
    assert error.value.code == 'COMMAND_TIMEOUT'


async def test_output_is_capped(tmp_path):
    _, _, _, tools = setup(tmp_path, 'full', command_max_output_chars=600)
    command = f'"{sys.executable}" -c "print(chr(120)*2000)"'
    result = await tools.execute(CommandArgs(command=command))
    assert result['truncated'] is True
    assert len(result['stdout']) <= 600


async def test_cwd_is_resolved_and_must_exist(tmp_path):
    _, _, _, tools = setup(tmp_path, 'full')
    sub = tmp_path / 'sub'
    sub.mkdir()
    result = await tools.execute(CommandArgs(command='cd' if sys.platform == 'win32' else 'pwd', cwd=str(sub)))
    assert str(sub) in result['stdout']
    with pytest.raises(AppError) as error:
        await tools.execute(CommandArgs(command=echo('hi'), cwd=str(tmp_path / 'missing')))
    assert error.value.code == 'COMMAND_CWD_INVALID'


async def test_disabled_mode_has_no_spec_and_rejects(tmp_path):
    _, _, _, tools = setup(tmp_path, 'disabled')
    assert tools.specs() == []
    with pytest.raises(AppError):
        await tools.execute(CommandArgs(command=echo('hi')))


async def test_cancelling_the_tool_terminates_the_running_command(tmp_path):
    _, _, _, tools = setup(tmp_path, 'full', command_timeout_seconds=30)
    marker = tmp_path / 'still-running.txt'
    if sys.platform == 'win32':
        command = 'ping -n 4 127.0.0.1 > nul && echo late > "' + str(marker) + '"'
    else:
        command = 'sleep 3; echo late > "' + str(marker) + '"'
    task = asyncio.create_task(tools.execute(CommandArgs(command=command)))
    await asyncio.sleep(0.5)
    task.cancel()
    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Stop/turn deadline must not leave the command running in a worker thread.
    assert asyncio.get_running_loop().time() - started < 3
    await asyncio.sleep(3.5)
    assert not marker.exists()


def test_command_spec_timeout_outlasts_the_command_timeout(tmp_path):
    _, _, _, tools = setup(tmp_path, 'full', command_timeout_seconds=40, tool_timeout_seconds=15)
    spec = tools.specs()[0]
    assert spec.execution_timeout(15) == 50

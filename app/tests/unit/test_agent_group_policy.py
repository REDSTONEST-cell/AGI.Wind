from uuid import uuid4

from agi_wind.agent.delivery import agent_execution_policy
from agi_wind.agent.settings import AgentSettings
from agi_wind.core.types import SceneScope


def scope(scene="owner_private", room_id=None):
    return SceneScope(account_id=uuid4(), user_id=uuid4(), companion_id=uuid4(),
                      conversation_id=uuid4(), scene=scene, room_id=room_id)


def test_group_scene_never_inherits_owner_local_file_or_command_access():
    settings = lambda: AgentSettings(enabled=True, file_access_mode='full', command_access_mode='full')
    private_policy, private_local = agent_execution_policy(settings, scope())
    assert private_policy.file_access_mode == 'full'
    assert private_policy.command_access_mode == 'full'
    assert private_local is True
    group_policy, group_local = agent_execution_policy(settings, scope('group', uuid4()))
    assert group_policy.file_access_mode == 'disabled'
    assert group_policy.command_access_mode == 'disabled'
    assert group_local is False


def test_group_scene_keeps_only_non_local_tool_switches_unchanged():
    settings = lambda: AgentSettings(enabled=True, memory_search_enabled=True, sticker_search_enabled=True,
                                     sticker_send_enabled=True, web_search_enabled=True,
                                     file_access_mode='ask', command_access_mode='ask')
    group_policy, group_local = agent_execution_policy(settings, scope('group', uuid4()))
    assert group_local is False
    assert group_policy.memory_search_enabled and group_policy.sticker_search_enabled
    assert group_policy.sticker_send_enabled and group_policy.web_search_enabled

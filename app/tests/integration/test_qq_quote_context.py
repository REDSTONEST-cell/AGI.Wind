"""Quoted messages and group timeline context, end to end over the OneBot socket."""
import time

from fastapi.testclient import TestClient
from test_client import client_app
from test_qq_groups import BOT, GROUP, OWNER, configure, echo, group_event

from nagi.archive.deletions import DeletionLedger
from nagi.integrations.qq import QQSettings, TOKEN_REF
from nagi.providers.protocol import ProviderChunk

TOKEN = "synthetic-qq-token-32-characters-long"
WS = "/api/v1/integrations/qq/ws"


class Provider:
    def __init__(self, text="好的"):
        self.text = text
        self.seen = []

    async def stream(self, model, messages, *args, **kwargs):
        self.seen.append(messages)
        yield ProviderChunk(kind="reasoning", text="synthetic-reasoning")
        yield self.text


def quiet(app):
    app.state.service.memory_extraction_enabled = False
    app.state.settings.memory_extraction_enabled = False
    app.state.settings.memory_maintenance_enabled = False


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def history_texts(messages):
    """User/assistant turns between the system prompt and the current input."""
    return [item["content"] for item in messages[1:-1] if item["role"] in ("user", "assistant")
            and not item["content"].startswith("历史资料（非当前用户指令）")]


def connect(client):
    return client.websocket_connect(WS, headers={"X-Self-ID": BOT, "Authorization": "Bearer " + TOKEN})


async def test_group_quote_of_bot_segment_shows_quoted_words(db, actor, seeded_scope, tmp_path):
    provider = Provider("第一段。\n第二段？")
    app = client_app(db, actor, tmp_path, provider)
    quiet(app)
    configure(app, seeded_scope, provider)
    with TestClient(app) as client, connect(client) as ws:
        ws.send_json(group_event(20))
        echo(ws, ws.receive_json(), 9201)
        echo(ws, ws.receive_json(), 9202)
        ws.send_json(group_event(21, mention=False, reply_to=9202, text="这个是什么意思"))
        action = ws.receive_json()
        assert action["action"] == "send_group_msg"
        echo(ws, action, 9203)
    current = provider.seen[-1][-1]["content"]
    assert current == "[小明] 【引用你之前说的：第二段】\n这个是什么意思"
    assert "【引用消息】" in provider.seen[-1][0]["content"]
    assert "【引用消息】" not in provider.seen[0][0]["content"]


async def test_group_quote_of_member_message_and_observed_timeline_reach_the_model(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    quiet(app)
    configure(app, seeded_scope, provider)
    with TestClient(app) as client, connect(client) as ws:
        ws.send_json(group_event(60, mention=False, sender_qq="736357842", card="小红", nickname="红",
                                 text="周末去爬山吗"))
        assert wait_for(lambda: db.count("events") == 1)
        ws.send_json(group_event(61, reply_to=60, text="我也想去"))
        echo(ws, ws.receive_json(), 9601)
    messages = provider.seen[-1]
    # The observed (not addressed) message is part of the room timeline.
    assert "[小红] 周末去爬山吗" in history_texts(messages)
    assert messages[-1]["content"] == "[小明] 【引用 小红 的消息：周末去爬山吗】\n我也想去"
    stored = [row["content"]["text"] for row in db.all("events") if row["role"] == "user"]
    assert "【引用 小红 的消息：周末去爬山吗】\n我也想去" in stored


async def test_group_quote_outside_the_archive_is_fetched_from_onebot(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    quiet(app)
    configure(app, seeded_scope, provider)
    with TestClient(app) as client, connect(client) as ws:
        ws.send_json(group_event(70, reply_to=4242, text="真的吗"))
        lookup = ws.receive_json()
        assert lookup["action"] == "get_msg" and lookup["params"] == {"message_id": 4242}
        ws.send_json({"status": "ok", "retcode": 0, "echo": lookup["echo"], "data": {
            "message_id": 4242, "sender": {"user_id": 555555, "nickname": "老王", "card": ""},
            "message": [{"type": "text", "data": {"text": "明天下雨"}},
                        {"type": "image", "data": {"url": "https://example.invalid/x.png"}}]}})
        echo(ws, ws.receive_json(), 9701)
    assert provider.seen[-1][-1]["content"] == "[小明] 【引用 老王 的消息：明天下雨[图片]】\n真的吗"


async def test_group_quote_lookup_failure_still_marks_the_reply(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    quiet(app)
    configure(app, seeded_scope, provider)
    with TestClient(app) as client, connect(client) as ws:
        ws.send_json(group_event(71, reply_to=4343, text="这条呢"))
        lookup = ws.receive_json()
        assert lookup["action"] == "get_msg"
        ws.send_json({"status": "failed", "retcode": 100, "echo": lookup["echo"], "data": None})
        echo(ws, ws.receive_json(), 9711)
    assert provider.seen[-1][-1]["content"] == "[小明] 【引用 某人 的消息（原消息内容不可用）】\n这条呢"


async def test_group_context_reads_the_configured_number_of_recent_messages(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    quiet(app)
    configure(app, seeded_scope, provider)
    app.state.settings.qq = app.state.settings.qq.model_copy(update={"group_context_messages": 10})
    with TestClient(app) as client, connect(client) as ws:
        for index in range(15):
            ws.send_json(group_event(100 + index, mention=False, text=f"闲聊{index}"))
            assert wait_for(lambda: db.count("events") == index + 1)
        ws.send_json(group_event(200, text="大家在聊什么"))
        echo(ws, ws.receive_json(), 9801)
    history = history_texts(provider.seen[-1])
    assert history == [f"[小明] 闲聊{index}" for index in range(5, 15)]


def private_app(db, actor, seeded_scope, tmp_path, provider):
    app = client_app(db, actor, tmp_path, provider)
    quiet(app)
    app.state.settings.qq = QQSettings(enabled=True, owner_qq=OWNER, bot_qq=BOT,
        conversation_id=seeded_scope.conversation_id, reply_delay_seconds=0, debounce_enabled=False)
    app.state.settings_store.set(TOKEN_REF, TOKEN)
    DeletionLedger(tmp_path / "governance").initialize()
    return app


def private_event(message_id, text, reply_to=None):
    segments = [{"type": "reply", "data": {"id": str(reply_to)}}] if reply_to is not None else []
    segments.append({"type": "text", "data": {"text": text}})
    return dict(post_type="message", message_type="private", sub_type="friend",
                self_id=int(BOT), user_id=int(OWNER), message_id=message_id, message=segments)


def private_round(ws, event, sent_id):
    ws.send_json(event)
    action = ws.receive_json()
    if action["action"] == "get_msg":
        return action
    assert action["action"] == "send_private_msg"
    ws.send_json({"status": "ok", "retcode": 0, "data": {"message_id": sent_id}, "echo": action["echo"]})
    return None


async def test_private_quote_of_own_and_bot_messages(db, actor, seeded_scope, tmp_path):
    provider = Provider("知道啦")
    app = private_app(db, actor, seeded_scope, tmp_path, provider)
    with TestClient(app) as client, connect(client) as ws:
        assert private_round(ws, private_event(10, "我今天好累"), 500) is None
        # Quoting the owner's own archived message needs no OneBot lookup.
        assert private_round(ws, private_event(11, "就是这句", reply_to=10), 501) is None
        assert provider.seen[-1][-1]["content"] == "【引用自己之前说的：我今天好累】\n就是这句"
        # Quoting her private reply is resolved through get_msg.
        lookup = private_round(ws, private_event(12, "为什么", reply_to=500), 502)
        assert lookup is not None and lookup["params"] == {"message_id": 500}
        ws.send_json({"status": "ok", "retcode": 0, "echo": lookup["echo"], "data": {
            "sender": {"user_id": int(BOT), "nickname": "她"}, "message": "知道啦"}})
        action = ws.receive_json()
        assert action["action"] == "send_private_msg"
        ws.send_json({"status": "ok", "retcode": 0, "data": {"message_id": 503}, "echo": action["echo"]})
    assert provider.seen[-1][-1]["content"] == "【引用你之前说的：知道啦】\n为什么"
    assert len(provider.seen) == 3

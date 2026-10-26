"""Rule-first group attention: when to reply, when to observe.

Strong signals (explicit @ or a quote of a message this bot actually sent) are decided
without any model. Only a message that arrives shortly after a successful reply, inside
the configured round limit and carrying a clear continuation cue, is treated as a
follow-up. Everything else stays observed context and costs no model call.

There is deliberately no identity decision here: whether someone is the owner comes
from the server-side QQ number match, never from message text.
"""
import re
import time
from dataclasses import dataclass, field

# Explicit continuation wording only. A quiet group message must not become a reply
# merely because it arrived inside the time window.
_FOLLOWUP = re.compile(
    r"为什么|为啥|怎么|如何|那[^，。！？,.!?]{0,4}呢|然后|后来|还有|继续|接着|"
    r"再发|再来|再说|再讲|是不是|真的吗|你说|你是|你刚|你刚才|你之前|"
    r"[？?]|吗[。！!…]*$|呢[。！!…]*$|对不对|好不好|行不行|可以吗|啥意思|什么意思"
)


@dataclass(frozen=True)
class ReplyDecision:
    action: str  # reply | observe
    reason: str
    thread_id: str | None = None
    target_principal_id: str | None = None


@dataclass
class _Thread:
    room_key: str
    participants: set = field(default_factory=set)
    last_reply_at: float = 0.0
    rounds: int = 0
    last_bot_message_id: str | None = None


class GroupAttention:
    def __init__(self, followup_seconds=90, max_rounds=4, clock=None):
        self.followup_seconds = int(followup_seconds)
        self.max_rounds = int(max_rounds)
        self.clock = clock or time.monotonic
        self.threads: dict[str, _Thread] = {}

    def thread(self, room_key):
        thread = self.threads.get(room_key)
        if thread and self.clock() - thread.last_reply_at > self.followup_seconds:
            # Expiry returns the room to ordinary observation; the next @ starts fresh.
            self.threads.pop(room_key, None)
            return None
        return thread

    def decide(self, message, *, receipt_exists=False, trigger_mode="smart"):
        room_key = str(message.get("room_key") or "")
        sender = message.get("sender_principal_id")
        sender_key = str(sender) if sender else None
        if message.get("is_self"):
            return ReplyDecision("observe", "self_message")
        if message.get("platform_message_id") and message.get("duplicate"):
            return ReplyDecision("observe", "duplicate")
        mentioned = {str(value) for value in message.get("mentioned_qq_ids") or []}
        bot_qq = str(message.get("bot_qq") or "")
        if bot_qq and bot_qq in mentioned:
            return ReplyDecision("reply", "mention", room_key, sender_key)
        reply_to = message.get("reply_to_platform_message_id")
        if reply_to:
            if receipt_exists:
                return ReplyDecision("reply", "reply_to_bot", room_key, sender_key)
            return ReplyDecision("observe", "reply_to_other")
        if message.get("anonymous"):
            return ReplyDecision("observe", "anonymous")
        if trigger_mode == "mention":
            return ReplyDecision("observe", "mention_only_mode")
        thread = self.thread(room_key)
        if not thread:
            return ReplyDecision("observe", "no_active_thread")
        if mentioned - {bot_qq}:
            return ReplyDecision("observe", "addressed_other")
        if not sender_key:
            return ReplyDecision("observe", "unknown_sender")
        if thread.rounds >= self.max_rounds:
            return ReplyDecision("observe", "rounds_exhausted")
        text = str(message.get("text") or "").strip()
        if not _FOLLOWUP.search(text):
            return ReplyDecision("observe", "no_followup_cue")
        return ReplyDecision("reply", "follow_up", room_key, sender_key)

    def mark_replied(self, room_key, sender_principal_id=None, bot_message_ids=None):
        """Only a successful send starts/extends the short follow-up window."""
        room_key = str(room_key)
        thread = self.threads.get(room_key) or _Thread(room_key=room_key)
        fresh = not self.thread(room_key)
        thread.last_reply_at = self.clock()
        thread.rounds = 0 if fresh else thread.rounds + 1
        if sender_principal_id:
            thread.participants.add(str(sender_principal_id))
        if bot_message_ids:
            thread.last_bot_message_id = str(bot_message_ids[-1])
        self.threads[room_key] = thread

    def forget(self, room_key):
        self.threads.pop(str(room_key), None)

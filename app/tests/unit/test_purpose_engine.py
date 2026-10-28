"""Unit tests for the pure purpose engine, closure records and the storage-free service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nagi.purpose import (
    GUIDANCE,
    PRIORITY,
    ClosureRecord,
    InMemoryPurposeStore,
    PurposeDeps,
    PurposeService,
    PurposeSettings,
    as_closure,
    blocking_reason,
    build_closure,
    closure_material,
    consume_closure,
    decide,
    from_decision,
    is_expired,
)

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)


def enabled(**overrides: object) -> PurposeSettings:
    payload: dict[str, object] = {"enabled": True}
    payload.update(overrides)
    return PurposeSettings(**payload)


def user(text: str, *, hours_ago: float = 2, event_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "role": "user",
        "content": {"text": text},
        "occurred_at": NOW - timedelta(hours=hours_ago),
        "id": event_id or f"e-{abs(hash(text)) % 100000}",
    }
    return payload


def assistant(text: str, *, purpose: str | None = None, nested: bool = False) -> dict[str, object]:
    content: dict[str, object] = {"text": text}
    if purpose is not None:
        if nested:
            content["proactive"] = {"purpose": purpose}
        else:
            content["purpose"] = purpose
    return {"role": "assistant", "content": content, "occurred_at": NOW - timedelta(minutes=30), "id": "a1"}


def memory(kind: str, text: str, *, memory_id: str = "m1") -> dict[str, object]:
    return {"id": memory_id, "kind": kind, "content": text, "created_at": NOW - timedelta(hours=5)}


# ------------------------------------------------------------------ engine


def test_priority_order_is_explicit() -> None:
    assert PRIORITY == ("follow_up", "care", "curiosity", "invite", "share")
    assert set(GUIDANCE) == set(PRIORITY)


def test_no_material_returns_none() -> None:
    assert decide(NOW, settings=enabled()) is None
    assert blocking_reason(NOW, settings=enabled()) == "no_material"


def test_care_for_wellbeing_message() -> None:
    events = [user("今天加班好累，感觉撑不住了")]
    decision = decide(NOW, recent_events=events, settings=enabled())
    assert decision is not None
    assert decision.purpose == "care"
    assert decision.reason == "user_wellbeing"
    assert "累" in decision.seed
    assert decision.topic
    assert decision.source_event_ids == [events[0]["id"]]


def test_follow_up_beats_care() -> None:
    events = [user("最近好累"), user("明天面试好紧张")]
    decision = decide(NOW, recent_events=events, settings=enabled())
    assert decision is not None
    assert decision.purpose == "follow_up"
    assert decision.reason == "open_loop"


def test_curiosity_for_unanswered_question() -> None:
    events = [user("你最近在听什么歌吗？")]
    decision = decide(NOW, recent_events=events, settings=enabled())
    assert decision is not None
    assert decision.purpose == "curiosity"
    assert decision.reason == "unanswered_question"


def test_curiosity_skips_answered_question() -> None:
    events = [
        user("你最近在听什么歌吗？"),
        assistant("在听一张老唱片。"),
    ]
    assert decide(NOW, recent_events=events, settings=enabled()) is None


def test_invite_from_preference_memory() -> None:
    memories = [memory("preference", "周末想一起探店")]
    decision = decide(NOW, memories=memories, settings=enabled())
    assert decision is not None
    assert decision.purpose == "invite"
    assert decision.reason == "shared_interest"
    assert decision.source_memory_ids == ["m1"]


def test_share_from_episode_memory() -> None:
    memories = [memory("episode", "昨晚读到一本很有趣的书")]
    decision = decide(NOW, memories=memories, settings=enabled())
    assert decision is not None
    assert decision.purpose == "share"
    assert decision.reason == "episode_memory"


def test_plain_fact_is_not_share_material() -> None:
    memories = [memory("fact", "今天下了很久的雨")]
    assert decide(NOW, memories=memories, settings=enabled()) is None


def test_duplicate_purpose_is_suppressed() -> None:
    events = [assistant("最近还好吗", purpose="care"), user("今天加班好累")]
    assert decide(NOW, recent_events=events, settings=enabled()) is None
    assert blocking_reason(NOW, recent_events=events, settings=enabled()) == "duplicate_purpose"


def test_nested_proactive_marker_also_counts_as_delivered() -> None:
    events = [assistant("最近还好吗", purpose="care", nested=True), user("今天加班好累")]
    assert decide(NOW, recent_events=events, settings=enabled()) is None


def test_closure_candidate_wins_over_fresh_material() -> None:
    closure = build_closure(
        topic="那个问题",
        unanswered_question="你还没回答我",
        next_opening="上次问的海边",
        purpose="curiosity",
        now=NOW - timedelta(hours=1),
        ttl_hours=72,
    )
    assert closure is not None
    decision = decide(
        NOW,
        last_user_at=NOW - timedelta(hours=2),
        recent_events=[user("今天加班好累")],
        last_closure=closure,
        settings=enabled(),
    )
    assert decision is not None
    assert decision.purpose == "curiosity"
    assert decision.reason == "closure"
    assert "上次问的海边" in decision.seed
    assert decision.expires_at == closure.expires_at


def test_expired_closure_is_ignored() -> None:
    closure = build_closure(
        topic="旧话题",
        next_opening="上次的引子",
        purpose="care",
        now=NOW - timedelta(hours=5),
        ttl_hours=1,
    )
    assert closure is not None
    assert decide(NOW, last_closure=closure, settings=enabled()) is None


def test_closure_is_ignored_after_user_replied() -> None:
    closure = build_closure(
        topic="旧话题",
        next_opening="上次的引子",
        purpose="care",
        now=NOW - timedelta(hours=1),
        ttl_hours=72,
    )
    assert closure is not None
    decision = decide(
        NOW,
        last_user_at=NOW - timedelta(minutes=30),
        last_closure=closure,
        settings=enabled(),
    )
    assert decision is None


def test_closure_mapping_is_accepted() -> None:
    payload = {
        "topic": "旧话题",
        "next_opening": "上次的引子",
        "purpose": "follow_up",
        "created_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
    }
    decision = decide(NOW, last_closure=payload, settings=enabled())
    assert decision is not None
    assert decision.reason == "closure"


def test_decision_fields_are_explainable_and_bounded() -> None:
    events = [user("明天面试好紧张", event_id="e1")]
    decision = decide(NOW, recent_events=events, settings=enabled())
    assert decision is not None
    assert decision.guidance == GUIDANCE["follow_up"]
    assert decision.dedupe_key.startswith("follow_up:")
    assert decision.created_at == NOW
    assert decision.expires_at is None

    long = decide(
        NOW,
        recent_events=[user("最近加班好累" * 40)],
        settings=enabled(),
    )
    assert long is not None
    assert len(long.topic) <= 120
    assert len(long.seed) <= 160


# ----------------------------------------------------------------- closure


def test_build_closure_normalises_fields() -> None:
    record = build_closure(
        topic="  加班  ",
        unanswered_question=" 你还好吗 ",
        next_opening=" 下次问问 ",
        purpose="care",
        now=NOW,
        ttl_hours=2,
    )
    assert isinstance(record, ClosureRecord)
    assert record.topic == "加班"
    assert record.unanswered_question == "你还好吗"
    assert record.next_opening == "下次问问"
    assert record.purpose == "care"
    assert record.created_at == NOW
    assert record.expires_at == NOW + timedelta(hours=2)


def test_build_closure_rejects_bad_input() -> None:
    assert build_closure(topic="   ", now=NOW) is None
    assert build_closure(topic="话题", now=NOW, ttl_hours=0) is None
    assert build_closure(topic="话题", now=NOW, ttl_hours="nonsense") is None


def test_build_closure_unknown_purpose_falls_back() -> None:
    record = build_closure(topic="话题", purpose="unknown", now=NOW)
    assert record is not None
    assert record.purpose == "follow_up"


def test_build_closure_clamps_topic() -> None:
    record = build_closure(topic="x" * 500, now=NOW)
    assert record is not None
    assert len(record.topic) == 120


def test_from_decision_builds_closure() -> None:
    curiosity = SimpleNamespace(purpose="curiosity", topic="问题", seed="他问的那个问题", created_at=NOW)
    record = from_decision(curiosity, now=NOW, ttl_hours=3)
    assert record is not None
    assert record.purpose == "curiosity"
    assert record.unanswered_question == "他问的那个问题"
    assert record.next_opening == "他问的那个问题"

    care = SimpleNamespace(purpose="care", topic="关心", seed="他最近很累", created_at=NOW)
    care_record = from_decision(care, now=NOW)
    assert care_record is not None
    assert care_record.unanswered_question == ""
    assert care_record.next_opening == "他最近很累"
    assert from_decision(None, now=NOW) is None


def test_as_closure_accepts_mapping_and_object() -> None:
    mapping = {
        "topic": "话题",
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    assert as_closure(mapping) is not None
    obj = SimpleNamespace(topic="话题", created_at=NOW, expires_at=NOW + timedelta(hours=1))
    assert as_closure(obj) is not None


def test_as_closure_returns_none_for_malformed_values() -> None:
    assert as_closure(None) is None
    assert as_closure({"topic": ""}) is None
    assert as_closure({"created_at": "not-a-date", "topic": "x", "expires_at": NOW}) is None


def test_is_expired_boundary() -> None:
    record = build_closure(topic="话题", now=NOW, ttl_hours=1)
    assert record is not None
    assert is_expired(record, NOW) is False
    assert is_expired(record, NOW + timedelta(hours=1)) is True
    assert is_expired(None, NOW) is True


def test_consume_closure_handles_naive_timestamps() -> None:
    record = build_closure(topic="话题", now=datetime(2026, 10, 28, 10, 0), ttl_hours=1)
    assert record is not None
    assert consume_closure(record, NOW) is not None
    assert consume_closure(record, NOW + timedelta(hours=2)) is None


def test_closure_material_prefers_next_opening() -> None:
    record = build_closure(topic="话题", unanswered_question="问题", next_opening="引子", now=NOW)
    assert record is not None
    assert closure_material(record) == "引子"
    fallback = build_closure(topic="话题", unanswered_question="问题", now=NOW)
    assert fallback is not None
    assert closure_material(fallback) == "问题"


# ----------------------------------------------------------------- service


def service(
    store: object,
    *,
    settings: PurposeSettings | None = None,
    timezone: str = "UTC",
    recent_event_limit: int = 40,
) -> PurposeService:
    return PurposeService(
        PurposeDeps(
            store=store,  # type: ignore[arg-type]
            settings=settings or enabled(),
            clock=lambda: NOW,
            timezone=timezone,
            recent_event_limit=recent_event_limit,
        )
    )


def store_with_care() -> InMemoryPurposeStore:
    store = InMemoryPurposeStore()
    store.add_event("user", "今天加班好累", at=NOW - timedelta(hours=2))
    return store


def test_service_tick_returns_decision_and_records_closure() -> None:
    store = store_with_care()
    svc = service(store)
    check = svc.check()
    assert check.allowed is True
    assert check.reason == "ok"
    assert check.decision is not None
    assert check.decision.purpose == "care"
    assert check.sent_today == 0
    assert check.has_closure is False

    decision = svc.tick()
    assert decision is not None
    assert decision.purpose == "care"
    assert len(store.decisions) == 1
    closure = store.last_closure(NOW)
    assert closure is not None
    assert closure.topic == decision.topic
    assert closure.next_opening == decision.seed
    assert store.last_closure(NOW).expires_at == NOW + timedelta(hours=72)  # type: ignore[union-attr]


def test_service_check_and_preview_have_no_side_effects() -> None:
    store = store_with_care()
    svc = service(store)
    assert svc.preview() is not None
    assert store.decisions == []
    assert store.last_closure(NOW) is None


def test_service_tick_disabled_returns_none() -> None:
    store = store_with_care()
    svc = service(store, settings=PurposeSettings())
    assert svc.tick() is None
    assert svc.check().reason == "disabled"
    assert store.decisions == []


def test_service_tick_respects_gap() -> None:
    store = store_with_care()
    store.record_delivery(NOW - timedelta(minutes=10))
    svc = service(store)
    assert svc.tick() is None
    assert svc.check().reason == "gap"


def test_service_tick_respects_daily_cap() -> None:
    store = store_with_care()
    store.record_delivery(NOW - timedelta(hours=4))
    store.record_delivery(NOW - timedelta(hours=3))
    svc = service(store, settings=enabled(max_per_day=2))
    assert svc.tick() is None
    assert svc.check().reason == "daily_cap"


def test_service_uses_local_wall_clock_for_quiet_hours() -> None:
    store = store_with_care()
    svc = service(store, timezone="Asia/Shanghai")
    # 16:00 UTC is 00:00 in Shanghai, inside the default 23:30-08:30 quiet window.
    late = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    check = svc.check(late)
    assert check.allowed is False
    assert check.reason == "quiet_hours"


def test_service_confirm_saves_closure_after_delivery() -> None:
    store = store_with_care()
    svc = service(store)
    decision = svc.tick()
    assert decision is not None
    closure = svc.confirm(decision, now=NOW + timedelta(minutes=1))
    assert closure is not None
    assert store.last_closure(NOW) is not None
    assert svc.confirm(None) is None
    assert len(store.decisions) == 1


def test_service_confirm_with_closure_disabled_returns_none() -> None:
    store = store_with_care()
    svc = service(store, settings=enabled(closure_enabled=False))
    decision = svc.tick()
    assert decision is not None
    assert store.last_closure(NOW) is None
    assert svc.confirm(decision, now=NOW) is None


def test_service_clear_closure() -> None:
    store = store_with_care()
    svc = service(store)
    assert svc.tick() is not None
    assert svc.check().has_closure is True
    svc.clear_closure()
    assert store.last_closure(NOW) is None
    assert svc.check().has_closure is False


def test_service_tick_consumes_stored_closure_once() -> None:
    store = InMemoryPurposeStore()
    closure = build_closure(
        topic="旧话题",
        next_opening="上次的引子",
        purpose="follow_up",
        now=NOW - timedelta(hours=1),
        ttl_hours=72,
    )
    assert closure is not None
    store.save_closure(closure)
    svc = service(store)
    decision = svc.tick()
    assert decision is not None
    assert decision.reason == "closure"
    assert store.last_closure(NOW) is None
    # The single stored opening was used: no replay on the next tick.
    assert svc.tick(NOW + timedelta(minutes=1)) is None


def test_service_closure_is_revisited_after_delivery() -> None:
    store = store_with_care()
    svc = service(store)
    first = svc.tick()
    assert first is not None and first.purpose == "care"
    # The host delivered it and recorded the assistant turn with the same purpose.
    store.add_event("assistant", "辛苦了", at=NOW - timedelta(hours=1), purpose="care")
    revisited = svc.preview()
    assert revisited is not None
    assert revisited.purpose == "care"
    assert revisited.reason == "closure"


def test_service_recent_event_limit_is_applied() -> None:
    store = InMemoryPurposeStore()
    store.add_event("user", "今天加班好累", at=NOW - timedelta(hours=3))
    store.add_event("user", "你最近在听什么歌吗？", at=NOW - timedelta(hours=2))
    limited = service(store, recent_event_limit=1)
    assert limited.preview() is not None
    assert limited.preview().purpose == "curiosity"  # type: ignore[union-attr]
    full = service(store, recent_event_limit=10)
    assert full.preview() is not None
    assert full.preview().purpose == "care"  # type: ignore[union-attr]


def test_service_works_without_closure_or_audit_methods() -> None:
    class MinimalStore:
        def __init__(self) -> None:
            self.events = [{"role": "user", "content": {"text": "今天加班好累"}}]

        def last_user_at(self) -> datetime:
            return NOW - timedelta(hours=2)

        def last_delivery_at(self) -> None:
            return None

        def sent_today(self, now: datetime) -> int:
            return 0

        def recent_events(self, limit: int) -> list[dict[str, object]]:
            return self.events[-limit:]

        def memories(self, limit: int) -> list[object]:
            return []

    store = MinimalStore()
    svc = PurposeService(PurposeDeps(store=store, settings=enabled(), clock=lambda: NOW))  # type: ignore[arg-type]
    check = svc.check()
    assert check.allowed is True
    assert check.has_closure is False
    decision = svc.tick()
    assert decision is not None
    assert svc.confirm(decision, now=NOW) is None
    svc.clear_closure()  # must not raise


def test_service_unknown_timezone_raises() -> None:
    with pytest.raises(ValueError):
        PurposeDeps(store=InMemoryPurposeStore(), timezone="Mars/Olympus")


def test_service_negative_context_limit_raises() -> None:
    with pytest.raises(ValueError):
        PurposeDeps(store=InMemoryPurposeStore(), recent_event_limit=-1)


def test_inmemory_store_sent_today_ignores_other_days() -> None:
    store = InMemoryPurposeStore()
    yesterday = NOW - timedelta(days=1)
    store.record_delivery(yesterday)
    store.record_delivery(NOW - timedelta(hours=2))
    assert store.sent_today(NOW) == 1

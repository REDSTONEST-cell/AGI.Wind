from nagi.usage import ProcessMonitor, estimate_tokens


def test_estimate_tokens_counts_chinese_and_latin():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    # punctuation and whitespace do not add tokens; 3 CJK chars = 2 tokens
    assert estimate_tokens("你好，世界！") == 3
    # latin letters/digits: 10 visible chars -> 2.5 -> 3 after rounding
    assert estimate_tokens("hello world") == 3
    assert estimate_tokens("   ") == 0
    assert estimate_tokens("abc") == 1


def test_estimate_tokens_mixed_text_is_positive():
    value = estimate_tokens("记住 token 计算：中文约 1.5 字/Token")
    assert value > 0


def test_process_monitor_snapshot_shape():
    monitor = ProcessMonitor()
    snapshot = monitor.snapshot()
    assert set(snapshot) == {"uptime_seconds", "cpu_percent", "memory_bytes", "started_at"}
    assert snapshot["uptime_seconds"] >= 0
    assert 0 <= snapshot["cpu_percent"] <= 100
    assert snapshot["memory_bytes"] is None or snapshot["memory_bytes"] > 0

def test_token_limited_chunks_preserve_original_offsets():
    from agi_wind.memory.chunking import chunk_text
    text = "去年小王不喝咖啡，今年小王改喝茶。2026-10-25订了RTX4060。" * 18
    pieces = chunk_text(text, token_count=len, max_tokens=64, overlap=8)
    assert len(pieces) > 2
    assert all(text[p.start:p.end] == p.text and len(p.text) <= 64 for p in pieces)
    assert pieces[0].start == 0 and pieces[-1].end == len(text)
    assert all(right.start <= left.end for left, right in zip(pieces, pieces[1:]))

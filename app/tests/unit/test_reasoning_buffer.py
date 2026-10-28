from nagi.delivery.reasoning import ReasoningBuffer


async def test_buffer_batches_by_size_and_time_and_keeps_order():
    writes, now = [], [0.0]
    async def write(text): writes.append(text)
    buffer = ReasoningBuffer(write, max_chars=5, max_seconds=1, clock=lambda: now[0])
    for part in ('ab', 'cd'): await buffer.add(part)
    assert writes == []
    await buffer.add('ef')
    assert writes == ['abcdef']
    await buffer.add('g')
    now[0] = 2.0
    await buffer.add('h')
    assert writes == ['abcdef', 'gh']
    await buffer.add('')
    await buffer.add('i')
    await buffer.flush()
    await buffer.flush()
    assert writes == ['abcdef', 'gh', 'i']

from nagi.voice.call import CallTickets


def test_call_ticket_is_expiring_single_use_and_bounded():
    now = [10.0]
    tickets = CallTickets(clock=lambda: now[0], limit=2)
    first = tickets.issue('owner-a')
    assert tickets.consume(first) == 'owner-a'
    assert tickets.consume(first) is None
    expired = tickets.issue('owner-b')
    now[0] += 31
    assert tickets.consume(expired) is None
    tickets.issue('one')
    tickets.issue('two')
    tickets.issue('three')
    assert len(tickets.pending) == 2


def test_pcm_input_rejects_oversize_misaligned_and_nonfinite():
    import struct
    import pytest
    from nagi.voice.call import validate_pcm
    for data in (b'x', b'\0' * 12804, struct.pack('<f', float('nan')), struct.pack('<f', float('inf'))):
        with pytest.raises(ValueError):
            validate_pcm(data)
    assert validate_pcm(struct.pack('<ff', -0.5, 0.5)) is None

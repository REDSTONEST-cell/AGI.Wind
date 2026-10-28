from uuid import uuid4
import pytest

def test_candidate_requires_sources_and_epistemic_kind():
    from nagi.memory.extraction import MemoryCandidate
    from pydantic import ValidationError
    with pytest.raises(ValidationError): MemoryCandidate(kind="fact", content="生日是今天", epistemic_type="user_asserted", source_refs=[])

def test_trigger_coalesces_eight_turns_and_idle():
    from nagi.jobs.handlers import extraction_due
    assert extraction_due(completed_turns=8, idle_seconds=0)
    assert extraction_due(completed_turns=1, idle_seconds=120)
    assert not extraction_due(completed_turns=0, idle_seconds=120)
    assert not extraction_due(completed_turns=7, idle_seconds=119)

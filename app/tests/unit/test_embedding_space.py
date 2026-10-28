import pytest

def test_same_dimension_different_model_is_not_compatible():
    from nagi.memory.embedding import EmbeddingSpace
    first = EmbeddingSpace(model_name="model-a", revision="abc", weights_hash="a", tokenizer_hash="t", dimension=384)
    second = first.model_copy(update={"model_name": "model-b"})
    with pytest.raises(ValueError, match="fingerprint"):
        first.require_compatible(second)
    assert first.fingerprint != second.fingerprint

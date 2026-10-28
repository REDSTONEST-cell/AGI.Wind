import struct
from uuid import uuid4

import pytest


def test_ann_is_actual_graph_search_and_finds_synthetic_neighbors():
    import numpy as np
    from nagi.memory.ann import VectorIndex
    rng = np.random.default_rng(42)
    values = rng.normal(size=(2000, 512)).astype('float32')
    values /= np.linalg.norm(values, axis=1, keepdims=True)
    ids = [uuid4() for _ in values]
    index = VectorIndex()
    index.add(zip(ids, (row.tobytes() for row in values)))
    assert index.size == 2000
    for position in (0, 512, 1999):
        assert index.search(values[position], 10)[0] == ids[position]
    assert index.backend == 'usearch_hnsw'


def test_ann_rejects_corrupt_projection():
    from nagi.memory.ann import VectorIndex
    with pytest.raises(ValueError):
        VectorIndex().add([(uuid4(), struct.pack('<f', 1.0))])


def test_duplicate_vectors_do_not_disconnect_distinct_fact():
    from nagi.memory.ann import VectorIndex
    x = struct.pack('<512f', 1., *([0.] * 511))
    y = struct.pack('<512f', 0., 1., *([0.] * 510))
    index = VectorIndex()
    index.add([(i, x) for i in range(2000)] + [(2000, y)])
    assert index.size == 2001
    assert index.search([0., 1.] + [0.] * 510, 10)[0] == 2000


def test_predicate_aliases_only_fold_known_fact_slots():
    from nagi.memory.extraction import _semantic_predicate, _semantic_text
    assert _semantic_predicate(' 喜爱的饮料 ') == _semantic_predicate('喜欢的饮料')
    assert _semantic_predicate('favorite_drink') == _semantic_predicate('喜欢的饮料')
    assert _semantic_predicate('friend_name') != _semantic_predicate('name')
    assert _semantic_text('Café!') == 'café!'
    assert _semantic_text('不喜欢咖啡') != _semantic_text('喜欢咖啡')

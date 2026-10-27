"""Disposable in-process HNSW projection; PostgreSQL remains authoritative.

No files, model downloads or user text are held here. Native imports are lazy so
an unavailable binary only disables semantic retrieval, preserving keyword search.
"""
import math
import hashlib


class VectorIndex:
    backend = 'usearch_hnsw'

    def __init__(self):
        import numpy as np
        from usearch.index import Index
        self._np = np
        self._index = Index(ndim=512, metric='cos', dtype='f32',
                            connectivity=16, expansion_add=128, expansion_search=128)
        self._groups = []
        self._vector_keys = {}
        self._size = 0

    @property
    def size(self):
        return self._size

    def add(self, rows):
        keys, vectors = [], []
        for key, raw in rows:
            if len(raw) != 512 * 4:
                raise ValueError('invalid projection dimension')
            values = self._np.frombuffer(raw, dtype='<f4')
            if not self._np.isfinite(values).all() or self._np.linalg.norm(values) < 1e-12:
                raise ValueError('invalid projection values')
            fingerprint = hashlib.sha256(raw).digest()
            graph_key = self._vector_keys.get(fingerprint)
            if graph_key is None:
                graph_key = len(self._groups)
                self._vector_keys[fingerprint] = graph_key
                self._groups.append([])
                keys.append(graph_key)
                vectors.append(values)
            self._groups[graph_key].append(key)
            self._size += 1
        if keys:
            self._index.add(self._np.asarray(keys, dtype='uint64'),
                            self._np.asarray(vectors, dtype='float32'), threads=1)

    def search(self, vector, count):
        if not self.size:
            return []
        values = self._np.asarray(vector, dtype='float32')
        if values.shape != (512,) or not self._np.isfinite(values).all():
            raise ValueError('invalid query dimension or values')
        norm = float(self._np.linalg.norm(values))
        if not math.isfinite(norm) or norm < 1e-12:
            raise ValueError('invalid query norm')
        # Repeated exact embeddings share a graph node. Thousands of duplicate
        # nodes can otherwise crowd HNSW's neighbors and disconnect rare facts.
        matches = self._index.search(values / norm, min(count, len(self._groups)), threads=1)
        ids = []
        for key in matches.keys:
            ids.extend(sorted(self._groups[int(key)], key=str))
            if len(ids) >= count:
                break
        return ids[:count]

"""Explicit synthetic benchmark; never reads or writes a user's archive.

Run with app/.venv Python. --model opts into the separately running local service.
The 10k case measures only deterministic CPU vector ranking, not model quality or
database/whole-recall latency.
"""
import argparse
import asyncio
import json
import math
import random
import statistics
import time
from pathlib import Path


async def main():
    from agi_wind.memory.semantic import LocalClient, pack_vector, rank_vectors
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="store_true")
    args = parser.parse_args()
    rng = random.Random(42)
    query = [rng.uniform(-1,1) for _ in range(512)]
    rows = [(index,pack_vector([rng.uniform(-1,1) for _ in range(512)])) for index in range(10000)]
    rows[123] = (123,pack_vector(query))
    durations = []
    for _ in range(5):
        started = time.perf_counter()
        result = await asyncio.to_thread(rank_vectors, query, rows)
        durations.append((time.perf_counter()-started)*1000)
        assert result[0][0] == 123
    print(json.dumps({"fixture":"10000 synthetic 512-d normalized float32 vectors",
        "scope":"CPU deterministic cosine ranking only; no database, encoder, HTTP or user archive",
        "runs_ms":[round(t,2) for t in durations], "median_ms":round(statistics.median(durations),2)},ensure_ascii=False))
    if not args.model:
        return
    root = Path(__file__).resolve().parents[1]
    client = LocalClient(root/"config/memory.local.json",root/"data/memory-token.txt")
    started = time.perf_counter()
    docs = await client.embed(["我最爱喝咖啡，每天早上喝一杯。", "我周末喜欢去海边看海浪。", "这台路由器支持无线网络。"])
    document_ms = (time.perf_counter()-started)*1000
    times = []
    for _ in range(3):
        started = time.perf_counter()
        q = (await client.embed(["我平时偏爱哪种提神饮品？"],query=True))[0]
        times.append((time.perf_counter()-started)*1000)
    scores = [sum(a*b for a,b in zip(q,doc)) for doc in docs]
    assert max(range(len(scores)),key=scores.__getitem__) == 0
    assert all(len(doc)==512 and abs(sum(v*v for v in doc)-1)<0.0001 for doc in docs)
    print(json.dumps({"fixture":"3 synthetic Chinese documents and 1 paraphrased query",
        "scope":"real CPU service HTTP embedding smoke; not a retrieval quality benchmark",
        "fingerprint":client.space.fingerprint,"dimension":512,"normalized":True,
        "document_batch_ms":round(document_ms,2),"query_runs_ms":[round(t,2) for t in times],
        "cosine_scores":[round(s,4) for s in scores],"top_document":0},ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

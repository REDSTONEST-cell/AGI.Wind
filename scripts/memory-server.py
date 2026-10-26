"""Private loopback CPU embedding process. Setup is the only download path."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
MAX_BODY = 170000


def fingerprint(space):
    return hashlib.sha256(json.dumps(space, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def file_set_hash(paths, root):
    entries = [(str(path.relative_to(root)).replace("\\", "/"),
                hashlib.sha256(path.read_bytes()).hexdigest()) for path in sorted(paths)]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def model_metadata(model_path):
    files = [p for p in model_path.rglob("*") if p.is_file() and ".cache" not in p.parts]
    weights = [p for p in files if ".onnx" in p.name]
    tokenizer = [p for p in files if p not in weights and p.suffix in (".json", ".txt", ".model")]
    if not weights or not any(p.name == "tokenizer.json" for p in tokenizer):
        raise ValueError("incomplete local model artifacts")
    space = {"model_name": MODEL, "revision": "fastembed-"+importlib.metadata.version("fastembed"),
        "weights_hash": file_set_hash(weights, model_path), "tokenizer_hash": file_set_hash(tokenizer, model_path),
        "dimension": 512, "normalize": True, "metric": "cosine",
        "preprocessing": "bge-zh-v1:strip;query-prefix="+QUERY_PREFIX+";max-length=512;pool=cls;l2;onnxruntime="+importlib.metadata.version("onnxruntime")}
    return {"space": space, "fingerprint": fingerprint(space), "device":"cpu", "threads":2,
            "providers":["CPUExecutionProvider"]}


def require_cpu_session(encoder):
    session = encoder.model.model
    options = session.get_session_options()
    if (session.get_providers() != ["CPUExecutionProvider"]
        or options.intra_op_num_threads != 2 or options.inter_op_num_threads != 2):
        raise ValueError("memory model must use CPUExecutionProvider with two threads")


def create_app(encoder, metadata, token):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    busy = asyncio.Lock()

    @app.middleware("http")
    async def private_only(request: Request, call_next):
        if "origin" in request.headers:
            return JSONResponse({"error":"origin_rejected"}, status_code=403)
        if request.headers.get("host", "") not in ("127.0.0.1:8141", "localhost:8141"):
            return JSONResponse({"error":"host_rejected"}, status_code=403)
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer "+token):
            return JSONResponse({"error":"unauthorized"}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({"error":"payload_too_large"}, status_code=413)
        # Starlette replays the buffered request for JSON parsing downstream.
        request._body = bytes(body)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status":"ready", **metadata}

    @app.post("/embed")
    async def embed(request: Request):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            return JSONResponse({"error":"invalid_json"}, status_code=422)
        if not isinstance(data, dict):
            return JSONResponse({"error":"invalid_request"}, status_code=422)
        texts, query = data.get("texts"), data.get("query", False)
        if (set(data)-{"texts","query","fingerprint"} or not isinstance(query,bool)
            or not isinstance(texts,list) or not 1 <= len(texts) <= 64
            or (query and len(texts) != 1) or any(not isinstance(t,str) or not t.strip() or len(t)>4096 for t in texts)
            or sum(len(t) for t in texts) > 40000):
            return JSONResponse({"error":"invalid_request"}, status_code=422)
        if data.get("fingerprint", metadata["fingerprint"]) != metadata["fingerprint"]:
            return JSONResponse({"error":"fingerprint_mismatch"}, status_code=409)
        if busy.locked():
            return JSONResponse({"error":"busy"}, status_code=503)
        def encode():
            prepared = [(QUERY_PREFIX if query else "")+text.strip() for text in texts]
            vectors = []
            for vector in encoder.embed(prepared, batch_size=min(16,len(prepared))):
                row = [float(v) for v in vector]
                norm = math.sqrt(sum(v*v for v in row))
                if len(row) != 512 or norm < 1e-12 or not math.isfinite(norm):
                    raise ValueError("invalid model output")
                vectors.append([v/norm for v in row])
            if len(vectors) != len(texts):
                raise ValueError("invalid model count")
            return vectors
        async with busy:
            try:
                vectors = await asyncio.to_thread(encode)
            except Exception:
                return JSONResponse({"error":"embedding_unavailable"}, status_code=503)
        return {"fingerprint":metadata["fingerprint"], "vectors":vectors}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    cache = root / ".devtools/memory-model-cache"
    # Keep every download/runtime asset in this project's ignored cache. The
    # ordinary serve path is fully offline, even if a backend changes defaults.
    os.environ["HF_HOME"] = str(cache / "hf-home")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    if not args.setup:
        os.environ["HF_HUB_OFFLINE"] = "1"
    config_path = root / "config/memory.local.json"
    from fastembed import TextEmbedding
    if args.setup:
        encoder = TextEmbedding(model_name=MODEL, cache_dir=str(cache), threads=2,
                                providers=["CPUExecutionProvider"])
        require_cpu_session(encoder)
        model_path = Path(encoder.model._model_dir).resolve()
        metadata = model_metadata(model_path)
        # Validate real inference before marking installation ready.
        vector = next(encoder.embed(["本地中文语义模型安装检查"]))
        if len(vector) != 512:
            raise ValueError("unexpected model dimension")
        config = {"schema_version":1,"url":"http://127.0.0.1:8141", "model_path":str(model_path), **metadata}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        token_path = root / "data/memory-token.txt"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        print(json.dumps({"status":"installed", **metadata}, ensure_ascii=False))
        return
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    model_path = Path(config["model_path"]).resolve()
    metadata = model_metadata(model_path)
    if metadata["fingerprint"] != config["fingerprint"]:
        raise ValueError("local model bytes changed; rerun explicit setup")
    encoder = TextEmbedding(model_name=MODEL, specific_model_path=str(model_path), local_files_only=True,
        threads=2, providers=["CPUExecutionProvider"])
    require_cpu_session(encoder)
    token = (root / "data/memory-token.txt").read_text(encoding="utf-8-sig").strip()
    if len(token)<32:
        raise ValueError("invalid private memory token")
    import uvicorn
    uvicorn.run(create_app(encoder, metadata, token), host="127.0.0.1", port=8141,
                access_log=False, log_level="error", limit_concurrency=8, timeout_keep_alive=2)


if __name__ == "__main__":
    main()

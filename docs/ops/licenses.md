# 依赖与资产边界

精确版本见 app/uv.lock 和 pnpm-lock.yaml，保留安装包中的许可。原 Live2D、研究和工具资产未修改或并入新业务源码。

- PostgreSQL：PostgreSQL License；Windows 便携包来自 EDB，包内许可保留。
- Python：PSF；FastAPI/Starlette/SQLAlchemy/Alembic/Pydantic/HTTPX/Uvicorn/PyYAML/pytest 等以分发包许可为准。
- psycopg：LGPL-3.0 及相关包的许可文本；Pillow：HPND，以锁定分发包元数据为准。
- TypeScript：Apache-2.0；json-schema-to-typescript：MIT。
- Letta、GPT-SoVITS、BGE、SenseVoice、EverOS 本轮未装入本项目业务环境，锁版/权重/许可仍为后续任务。

发布或分发头像/模型前须核对具体素材授权；当前不包含其发布验收。

USearch 本地 HNSW Python 依赖采用 Apache-2.0，版本以 app/uv.lock 为准；本次仅调用公开接口。

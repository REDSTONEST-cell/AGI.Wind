from contextlib import asynccontextmanager
import asyncio
import json
import os
from pathlib import Path
from typing import Any,Literal
from uuid import UUID,uuid4
from fastapi import FastAPI,Depends,UploadFile,File,Header,Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse,StreamingResponse,FileResponse
from sqlalchemy import create_engine,select,text,func
from pydantic import Field
from ..core.settings import Settings,load_settings
from ..core.types import DTO,Actor,ChatRequest,ModelRef,Scope
from ..core.errors import AppError
from ..archive import models as m
from ..archive.repository import Database,authorized_conversation,lock
from ..archive.assets import AssetStore
from ..archive.exports import ExportService
from ..archive.imports import ImportService
from ..archive.deletions import DeletionLedger,DeletionService
from ..identity.service import IdentityService,authorize
from ..memory.retrieval import MemoryService
from ..memory.corrections import CorrectionService
from ..delivery.service import GenerationService,generation_scope,TERMINAL
from ..delivery.stream import encode_sse
from ..delivery import models as dm
from ..jobs.worker import Worker
from .auth import bearer_auth

class ConversationCreate(DTO): companion_id:UUID; title:str=""
class PersonaPatch(DTO): expected_version:int=Field(ge=1); patch:dict[str,Any]
class InterruptRequest(DTO): command_id:UUID
class SearchRequest(DTO): companion_id:UUID; conversation_id:UUID; query:str=Field(min_length=1,max_length=2000)
class CorrectionRequest(DTO): memory_id:UUID; correction_text:str; source_event_id:UUID
class ExportRequest(DTO): companion_id:UUID

def create_app(settings:Settings,db:Database,actor:Actor,generation_service=None):
    assets=AssetStore(db,settings.data_dir/"assets",max_bytes=settings.max_audio_bytes,max_image_bytes=settings.max_image_bytes)
    service=generation_service or GenerationService(db,None,runtime_name=settings.runtime.preferred)
    identity=IdentityService(db)
    ledger=DeletionLedger(settings.data_dir/"governance")
    deletion=DeletionService(db,ledger,assets.root)
    authenticate=bearer_auth(settings,actor)
    wakeup=asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        deletion.replay()
        # A process restart never automatically repeats a possibly paid request.
        with db.engine.begin() as conn:
            conn.execute(m.generations.update().where(m.generations.c.state.in_(["running","draining"])).values(state="interrupted",error_code="PROCESS_RESTART",delivery_state="stopped"))
        worker=Worker(db)
        closing=asyncio.Event()
        async def loop():
            async def handle(job):
                if job["event_type"]!="generation.requested": raise AppError("HANDLER_UNCONFIGURED","任务处理器未配置",503)
                generation_id=UUID(job["payload"]["generation_id"])
                ran=await asyncio.create_task(service.run(generation_id))
                with db.engine.connect() as conn: state=conn.scalar(select(m.generations.c.state).where(m.generations.c.id==generation_id))
                if not ran and state=="pending": raise AppError("GENERATION_QUEUED","同一角色正在回答",429)
            while not closing.is_set():
                jobs=worker.claim("gateway",batch_size=4)
                if jobs:
                    for job in jobs:
                        if closing.is_set(): break
                        await worker.run_job(job["id"],"gateway",handle)
                else:
                    wakeup.clear()
                    try: await asyncio.wait_for(wakeup.wait(),timeout=30)
                    except TimeoutError: pass
        task=asyncio.create_task(loop())
        try: yield
        finally:
            closing.set(); wakeup.set()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            if service.provider and hasattr(service.provider,"aclose"): await service.provider.aclose()
            registry=getattr(service,"registry",None)
            if registry: await registry.aclose()

    app=FastAPI(title="AGI.Wind Gateway",version="0.1.0",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.db,app.state.service,app.state.assets=db,service,assets

    @app.middleware("http")
    async def contain_internal_errors(request,call_next):
        try: return await call_next(request)
        except Exception:
            # Do not let the server print upstream/DB exception details containing private rows.
            return JSONResponse({"error":{"code":"INTERNAL_ERROR","message":"请求未完成，请检查服务状态","retryable":False}},status_code=500)

    @app.exception_handler(AppError)
    async def error(request,exc): return JSONResponse(exc.public(),status_code=exc.status)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc): return JSONResponse({"error":{"code":"INVALID_REQUEST","message":"请求字段或参数无效","retryable":False}},status_code=422)
    @app.exception_handler(Exception)
    async def internal_error(request,exc): return JSONResponse({"error":{"code":"INTERNAL_ERROR","message":"请求未完成，请检查服务状态","retryable":False}},status_code=500)

    @app.get("/",include_in_schema=False)
    def browser_home():
        return FileResponse(Path(__file__).with_name("home.html"),media_type="text/html; charset=utf-8",headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @app.get("/health/live")
    def live(): return {"status":"live"}
    @app.post("/api/v1/admin/shutdown")
    async def shutdown(current=Depends(authenticate)):
        callback=getattr(app.state,"request_shutdown",None)
        if callback is None: raise AppError("NOT_MANAGED","当前实例不支持托管停止",409)
        callback()
        return {"status":"stopping"}
    @app.get("/health/ready")
    def ready():
        try:
            with db.engine.connect() as conn: conn.execute(text("SELECT 1"))
            database="available"
        except Exception: database="unavailable"
        dialogue="configured" if service.provider and service.model else "unconfigured"
        dependencies={"database":database,"assets":"available" if assets.root.is_dir() else "unavailable","dialogue":dialogue,"runtime":service.runtime_name,"tts":"unconfigured","asr":"unconfigured","embedding":"keyword_only","letta":"not_verified"}
        usable=database=="available" and dialogue=="configured"
        return JSONResponse({"status":"ready" if usable else "degraded","dependencies":dependencies},status_code=200 if usable else 503)

    @app.get("/api/v1/capabilities")
    def capabilities(current=Depends(authenticate)):
        return {"protocol_version":"1.0","text":service.provider is not None,"image":service.vision is not None,"streaming":True,"tts":False,"asr":False,"live2d":False,"runtime":service.runtime_name,"embedding":"keyword_only","voice_training":"awaiting_material_and_gpu","archive_format_version":1}

    @app.get("/api/v1/companions")
    def companions(current=Depends(authenticate)):
        with db.engine.connect() as conn: return [dict(row) for row in conn.execute(select(m.companions).where(m.companions.c.account_id==current.account_id)).mappings()]

    @app.post("/api/v1/conversations",status_code=201)
    def conversation(body:ConversationCreate,current=Depends(authenticate)):
        with db.engine.begin() as conn:
            authorize(conn,current,body.companion_id)
            key=conn.execute(m.conversations.insert().values(account_id=current.account_id,user_id=current.user_id,companion_id=body.companion_id,title=body.title).returning(m.conversations.c.id)).scalar_one()
        return {"conversation_id":key}

    @app.post("/api/v1/chat/requests",status_code=202)
    async def chat(body:ChatRequest,current=Depends(authenticate)):
        accepted=await service.submit(current,body)
        wakeup.set()
        return accepted

    @app.get("/api/v1/generations/{generation_id}")
    def generation(generation_id:UUID,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            row,_,_=generation_scope(conn,generation_id,current)
            return {"generation_id":generation_id,"state":row["state"],"delivery_state":row["delivery_state"],"epoch":row["epoch"],"error_code":row["error_code"]}

    @app.get("/api/v1/generations/{generation_id}/events")
    async def events(generation_id:UUID,last_event_id:str|None=Header(default=None),after:int=0,current=Depends(authenticate)):
        try: cursor=int(last_event_id) if last_event_id is not None else after
        except ValueError: raise AppError("INVALID_SEQUENCE","事件序号无效",422)
        service.replay(current,generation_id,cursor)
        async def frames():
            nonlocal cursor
            while True:
                batch=service.replay(current,generation_id,cursor)
                for event in batch:
                    cursor=event.sequence
                    yield encode_sse(event)
                with db.engine.connect() as conn:
                    generation,_,_=generation_scope(conn,generation_id,current)
                if generation["state"] in TERMINAL: break
                if not batch: yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(frames(),media_type="text/event-stream",headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

    @app.get("/api/v1/conversations/{conversation_id}/live")
    async def control(conversation_id:UUID,after:int=0,current=Depends(authenticate)):
        with db.engine.connect() as conn: authorized_conversation(conn,current,conversation_id)
        async def frames():
            cursor=after
            while True:
                with db.engine.connect() as conn:
                    rows=list(conn.execute(select(dm.conversation_live_events).where(dm.conversation_live_events.c.conversation_id==conversation_id,dm.conversation_live_events.c.sequence>cursor).order_by(dm.conversation_live_events.c.sequence)).mappings())
                for row in rows:
                    cursor=row["sequence"]
                    payload=json.dumps(dict(row),ensure_ascii=False,default=str)
                    yield f"id: {cursor}\nevent: {row['type']}\ndata: {payload}\n\n"
                if not rows: yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(frames(),media_type="text/event-stream",headers={"Cache-Control":"no-store"})

    @app.post("/api/v1/generations/{generation_id}/interrupt")
    async def interrupt(generation_id:UUID,body:InterruptRequest,current=Depends(authenticate)):
        return await service.interrupt(current,generation_id,body.command_id)

    @app.get("/api/v1/conversations/{conversation_id}/events")
    def history(conversation_id:UUID,cursor:UUID|None=None,limit:int=Query(default=50,ge=1,le=200),current=Depends(authenticate)):
        with db.engine.connect() as conn:
            authorized_conversation(conn,current,conversation_id)
            query=select(m.events).where(m.events.c.conversation_id==conversation_id)
            if cursor:
                pivot=conn.execute(select(m.events.c.recorded_at).where(m.events.c.id==cursor,m.events.c.conversation_id==conversation_id)).scalar()
                if pivot is None: raise AppError("INVALID_CURSOR","分页位置无效",422)
                from sqlalchemy import tuple_
                query=query.where(tuple_(m.events.c.recorded_at,m.events.c.id)>tuple_(pivot,cursor))
            return [dict(row) for row in conn.execute(query.order_by(m.events.c.recorded_at,m.events.c.id).limit(limit)).mappings()]

    @app.post("/api/v1/assets",status_code=201)
    async def upload(file:UploadFile=File(),current=Depends(authenticate)):
        return await assets.put(current,file.file,file.content_type or "application/octet-stream")

    @app.get("/api/v1/assets/{asset_id}")
    def asset(asset_id:UUID,current=Depends(authenticate)):
        record,path=assets.get(current,asset_id)
        return FileResponse(path,media_type=record["mime"],headers={"Cache-Control":"private,no-store"})

    @app.get("/api/v1/companions/{companion_id}/persona")
    async def persona(companion_id:UUID,current=Depends(authenticate)): return await identity.snapshot(current,companion_id)

    @app.patch("/api/v1/companions/{companion_id}/persona")
    async def update_persona(companion_id:UUID,body:PersonaPatch,current=Depends(authenticate)): return await identity.update(current,companion_id,body.expected_version,body.patch)

    @app.post("/api/v1/companions/{companion_id}/model")
    async def bind_model(companion_id:UUID,body:ModelRef,current=Depends(authenticate)):
        if body.provider_id not in settings.providers: raise AppError("UNKNOWN_PROVIDER","请先在服务端配置该厂商",422)
        if set(body.parameters)-set(settings.providers[body.provider_id].supported_parameters): raise AppError("UNSUPPORTED_PARAMETER","厂商不支持指定参数",422)
        return await identity.bind_model(current,companion_id,body)

    @app.post("/api/v1/memory/search")
    async def memory(body:SearchRequest,current=Depends(authenticate)):
        with db.engine.connect() as conn:
            conversation=authorized_conversation(conn,current,body.conversation_id)
            if conversation["companion_id"]!=body.companion_id: raise AppError("SCOPE_MISMATCH","角色与会话不匹配",403)
        scope=Scope(**current.model_dump(),companion_id=body.companion_id,conversation_id=body.conversation_id)
        await MemoryService(db).index_events(scope)
        return await MemoryService(db).search(scope,body.query)

    @app.post("/api/v1/memory/corrections")
    async def correct(body:CorrectionRequest,current=Depends(authenticate)):
        return await CorrectionService(db).apply_correction(current,body.memory_id,body.correction_text,body.source_event_id)

    @app.delete("/api/v1/events/{event_id}")
    async def delete_event(event_id:UUID,current=Depends(authenticate)): return await deletion.delete_event(current,event_id)

    @app.post("/api/v1/archive/exports",status_code=201)
    async def export(body:ExportRequest,current=Depends(authenticate)):
        key=uuid4()
        target=settings.data_dir/"exports"/(str(key)+".zip")
        await ExportService(db,assets.root).export(current,body.companion_id,target)
        return {"export_id":key,"download_url":f"/api/v1/archive/exports/{key}"}

    @app.get("/api/v1/archive/exports/{export_id}")
    def download_export(export_id:UUID,current=Depends(authenticate)):
        path=settings.data_dir/"exports"/(str(export_id)+".zip")
        if not path.is_file(): raise AppError("NOT_FOUND","导出包不存在",404)
        return FileResponse(path,media_type="application/zip",filename=path.name)

    @app.get("/api/v1/voices")
    def voices(current=Depends(authenticate)): return {"voices":[],"status":"unconfigured"}
    return app

def from_env():
    settings=load_settings(os.environ.get("AGI_CONFIG","../config/app.local.yaml"))
    from pydantic import SecretStr
    values=settings.model_dump()
    if os.environ.get("AGI_DATABASE_URL"): values["database_url"]=SecretStr(os.environ["AGI_DATABASE_URL"])
    elif (settings.data_dir/"local-db.json").exists(): values["database_url"]=SecretStr(json.loads((settings.data_dir/"local-db.json").read_text(encoding="utf-8"))["url"])
    if os.environ.get("AGI_ACCESS_TOKEN"): values["access_token"]=SecretStr(os.environ["AGI_ACCESS_TOKEN"])
    # Revalidate assigned secret fields and resolve only project-owned identity files.
    settings=Settings.model_validate(values)
    local=settings.data_dir/"local-identity.json"
    if not local.is_file(): raise RuntimeError("Run agi-wind init-local first")
    saved=json.loads(local.read_text(encoding="utf-8"))
    if settings.access_token is None:
        settings.access_token=__import__('pydantic').SecretStr((settings.data_dir/"access-token.txt").read_text(encoding="utf-8").strip())
    actor=Actor.model_validate(saved["actor"])
    engine=create_engine(settings.database_url.get_secret_value(),hide_parameters=True)
    db=Database(engine)
    from ..providers.registry import configured_provider,ProviderRegistry
    provider,model=configured_provider(settings,"dialogue") if settings.runtime.preferred=="direct_vendor" else (None,None)
    registry=ProviderRegistry(settings)
    service=GenerationService(db,provider,model=model,runtime_name=settings.runtime.preferred,provider_resolver=registry.resolve if settings.runtime.preferred=="direct_vendor" else None)
    service.registry=registry
    vision_provider,vision_model=configured_provider(settings,"vision")
    if vision_provider and vision_model:
        from ..providers.vision import VisionAdapter
        service.vision=VisionAdapter(db,AssetStore(db,settings.data_dir/"assets"),vision_provider.complete,vision_model)
    return create_app(settings,db,actor,service)
